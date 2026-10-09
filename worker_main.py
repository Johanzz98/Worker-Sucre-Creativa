import redis
import json
import logging
import time
import sys
import subprocess
import os
import shutil
import psycopg2
import urllib.parse
import urllib.request
import http.server
import socketserver
import threading
import numpy as np
import cv2
from psycopg2.extras import RealDictCursor
from pathlib import Path
from typing import Dict, List, Optional
from dotenv import load_dotenv

from app.core.uv_manager import UVMapManager
from app.application.render_frontback import render_frontback_designs

# boto3 opcional: solo necesario si se usa R2 para subir los mockups
try:
    import boto3
    BOTO3_AVAILABLE = True
except ImportError:
    BOTO3_AVAILABLE = False

# Cargar variables de entorno
load_dotenv()

# Asegurar UTF-8 en la consola (Windows usa cp1252 por defecto y los emojis rompen el logging)
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# Configuración de logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - [%(levelname)s] - %(name)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('mockup_worker.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# ============================================
# SERVIDOR HTTP PARA SERVIR RENDERS ESTÁTICOS
# ============================================
RENDER_PORT = 8001
RENDER_DIRECTORY = Path(__file__).parent / "data" / "blender-passes"

class CustomHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(RENDER_DIRECTORY), **kwargs)
    
    def log_message(self, format, *args):
        # Silenciar logs del servidor HTTP para no saturar
        pass
    
    def end_headers(self):
        # Agregar CORS headers para permitir acceso desde Node.js
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET')
        super().end_headers()

def run_render_server():
    """Ejecuta el servidor HTTP para servir archivos estáticos"""
    try:
        with socketserver.TCPServer(("", RENDER_PORT), CustomHandler) as httpd:
            logger.info(f"📁 Servidor de renders corriendo en http://localhost:{RENDER_PORT}")
            logger.info(f"📁 Sirviendo archivos desde: {RENDER_DIRECTORY}")
            httpd.serve_forever()
    except OSError as e:
        logger.error(f"❌ Error al iniciar servidor de renders (puerto {RENDER_PORT} en uso?): {e}")
    except Exception as e:
        logger.error(f"❌ Error en servidor de renders: {e}")

# Iniciar el servidor de renders en un hilo separado
render_thread = threading.Thread(target=run_render_server, daemon=True)
render_thread.start()
logger.info("🚀 Servidor de renders iniciado en hilo secundario")

class MockupWorker:
    def __init__(self, redis_url: str = None):
        # Conexión a Redis (usar REDIS_URL si está disponible)
        redis_url = redis_url or os.getenv("REDIS_URL", "redis://localhost:6379")
        self.redis_url = redis_url
        try:
            self.redis_client = redis.Redis.from_url(
                redis_url,
                decode_responses=True,
                socket_connect_timeout=10,
                socket_keepalive=True,
                retry_on_timeout=True,
            )
            self.redis_client.ping()
            logger.info(f"✅ Conectado a Redis: {redis_url}")
        except Exception as e:
            logger.error(f"❌ Error conectando a Redis: {e}")
            raise
        
        # Conexión a PostgreSQL (usar DATABASE_URL si está disponible)
        try:
            db_url = os.getenv("DATABASE_URL")
            if not db_url:
                db_host = os.getenv("DB_HOST")
                db_port = os.getenv("DB_PORT")
                db_name = os.getenv("DB_NAME")
                db_user = os.getenv("DB_USER")
                db_password = os.getenv("DB_PASSWORD")
                
                escaped_password = urllib.parse.quote_plus(db_password)
                db_url = f"postgresql://{db_user}:{escaped_password}@{db_host}:{db_port}/{db_name}"
            
            self.db_conn = psycopg2.connect(db_url)
            logger.info("✅ Conectado a PostgreSQL")
        except Exception as e:
            logger.error(f"❌ Error conectando a PostgreSQL: {e}")
            raise
        
        # Configuración R2 (opcional: subir mockups a Cloudflare R2)
        self.r2_config = {
            "endpoint": os.getenv("R2_ENDPOINT"),
            "access_key_id": os.getenv("R2_ACCESS_KEY_ID"),
            "access_key_secret": os.getenv("R2_ACCESS_KEY_SECRET"),
            "bucket": os.getenv("R2_BUCKET"),
            "public_url": os.getenv("R2_PUBLIC_URL"),
        }
        self.r2_enabled = all(self.r2_config.values()) and BOTO3_AVAILABLE
        if self.r2_enabled:
            self.s3_client = boto3.client(
                "s3",
                endpoint_url=self.r2_config["endpoint"],
                aws_access_key_id=self.r2_config["access_key_id"],
                aws_secret_access_key=self.r2_config["access_key_secret"],
                region_name="auto",
            )
            logger.info("✅ R2 storage habilitado para subir mockups")
        else:
            self.s3_client = None
            if BOTO3_AVAILABLE:
                logger.warning("⚠️ R2 no configurado (faltan variables o bucket), usando almacenamiento local")
            else:
                logger.warning("⚠️ boto3 no instalado, R2 deshabilitado (pip install boto3)")
        
        self.render_url = os.getenv("RENDER_URL", "http://localhost:8000")

        # Modo de generación de mockups: 'compositor' (UVMap + repintado) o
        # 'blender' (render por partes: textura horneada + re-render). En modo
        # 'blender' los mockups FRENTE/BACK se ven idénticos al preview GLB.
        self.render_mode = os.getenv("MOCKUP_RENDER_MODE", "compositor").lower()
        logger.info(f"🎬 MOCKUP_RENDER_MODE = {self.render_mode}")
        
        # Configuración de rutas
        self.python_app_path = Path(__file__).parent.resolve()
        
        # Usar variable de entorno
        storage_path = os.getenv("STORAGE_PATH", "C:/Users/johan/Documents/mockups")
        self.base_storage_path = Path(storage_path)
        
        if not self.base_storage_path.exists():
            logger.warning(f"⚠️ Storage path no existe, creando: {self.base_storage_path}")
            self.base_storage_path.mkdir(parents=True, exist_ok=True)
        
        logger.info(f"📁 Python app path: {self.python_app_path}")
        logger.info(f"📁 Storage path: {self.base_storage_path}")
        logger.info("🚀 Worker de Mockups inicializado (MODO DINÁMICO)")

    def get_render_path(self, product: str, angle: str = "centro") -> Path:
        """
        Construye la ruta de renders dinámicamente
        Busca en: data/blender-passes/taza/{product}/{angle}/
        """
        if product.startswith("taza_"):
            return self.python_app_path / "data" / "blender-passes" / "taza" / product / angle
        else:
            return self.python_app_path / "data" / "blender-passes" / product / angle

    def get_angles_for_product(self, product: str) -> List[str]:
        """
        Detecta automáticamente los ángulos disponibles para un producto
        Escanea las subcarpetas que contienen Beauty.png e ID.png
        """
        # Construir la ruta base del producto (sin ángulo)
        if product.startswith("taza_"):
            product_folder = self.python_app_path / "data" / "blender-passes" / "taza" / product
        else:
            product_folder = self.python_app_path / "data" / "blender-passes" / product
        
        angles = []
        
        if not product_folder.exists():
            logger.warning(f"⚠️ Ruta de producto no existe: {product_folder}")
            return angles
        
        # Buscar subcarpetas de ángulos (centro, derecha, izquierda, etc.)
        for item in product_folder.iterdir():
            if item.is_dir():
                beauty = item / "Beauty.png"
                id_map = item / "ID.png"
                if beauty.exists() and id_map.exists():
                    angles.append(item.name)
                    logger.info(f"   ✓ Ángulo detectado: {item.name}")
        
        if not angles:
            logger.warning(f"⚠️ No se encontraron ángulos en: {product_folder}")
        
        return angles

    def get_uvmap_path(self, renders_path: Path) -> Path | None:
        """
        Busca el archivo UVMap en la carpeta de renders del ángulo.
        Soporta diferentes nombres: UVMap.exr, UVMap_centro.exr, etc.
        (Cada ángulo tiene su propio UVMap.)
        """
        candidates = ["UVMap.exr", "UVMap_centro.exr", "UVMap_derecha.exr", "UVMap_izquierda.exr"]

        for candidate in candidates:
            test_path = renders_path / candidate
            if test_path.exists():
                return test_path

        return None

    def get_zones_for_product(self, renders_path: Path, product: str = "") -> List[Dict]:
        """
        Detecta las zonas de impresión de un producto (multizona) escaneando
        el directorio de renders del ángulo.

        Una zona existe si aparece un ID_<key>.png (máscara occlusion-aware de
        esa zona en este ángulo) junto al UVMap unificado (UVMap.exr): ambos son
        el fragmento visible desde la cámara, así que el diseño se mapea con el
        mismo UVMap pero restringido a la máscara de cada zona.

        EXCEPCIÓN (gorra/chapita): usa UN solo ID.png (igual que la polera),
        con su único mesh de impresión (la banda) como zona.
        """
        zones = []
        uvmap = renders_path / "UVMap.exr"
        if not uvmap.exists():
            return zones
        try:
            if self._is_mask_based(product):
                id_file = renders_path / "ID.png"
                if id_file.exists():
                    zones.append({
                        "key": "banda",
                        "uvmap": str(uvmap),
                        "mask": str(id_file),
                    })
                    logger.info(f"🧩 Zona única ID.png para {product}: [banda]")
                return zones
            for mask_file in renders_path.glob("ID_*.png"):
                key = mask_file.stem.replace("ID_", "", 1)
                if not key or key == "unified":
                    continue
                zones.append({
                    "key": key,
                    "uvmap": str(uvmap),
                    "mask": str(mask_file),
                })
            if zones:
                logger.info(f"🧩 Zonas multizona detectadas en {renders_path}: "
                            f"{[z['key'] for z in zones]}")
            return zones
        except Exception as e:
            logger.error(f"❌ Error detectando zonas en {renders_path}: {e}")
            return []

    # Plan A (reuv_polera_separado.py): frente en la mitad izquierda del atlas,
    # atras en la derecha. Coincide con el aspect de los canvas del editor
    # (402x420 frente, 420x420 atras) para que el diseño llene la superficie.
    PLAN_A_REGIONS = {
        "frente": {"umin": 0.020, "umax": 0.480, "vmin": 0.259, "vmax": 0.741},
        "atras":  {"umin": 0.520, "umax": 0.980, "vmin": 0.270, "vmax": 0.730},
    }

    # Productos cuya única superficie de impresión es ELLA MISMA y cuya isla UV
    # NO respeta el atlas del plan A (mitades frente/atrás del polera). Se
    # componen por MÁSCARA (multizona): el diseño se mapea directo por (U,V) del
    # mesh imprimible (igual que el GLB del editor) y se restringe con la máscara
    # del objeto (ID.png en la carpeta del ángulo).
    # El matching es por SUBSTRING: el job llega con product=model_key
    # (ej. "Cylinder.00111", "chapa", "chapita") que puede variar según el asset,
    # pero la familia es la misma ("cylinder.001*"/"chapita*"/"chapa*").
    MASK_BASED_PRODUCTS = {"chapita", "Cylinder.001", "chapa"}

    @staticmethod
    def _is_mask_based(product: str) -> bool:
        p = (product or "").lower()
        return 'chapita' in p or 'cylinder.001' in p or 'chapa' in p

    def _product_folder(self, product: str) -> Path:
        if product.startswith("taza_"):
            return self.python_app_path / "data" / "blender-passes" / "taza" / product
        return self.python_app_path / "data" / "blender-passes" / product

    def _has_frontback_masters(self, product: str) -> bool:
        """True si el producto tiene los dos masters de superficie (frente/atras),
        es decir el modelo per-mesh del polera."""
        folder = self._product_folder(product)
        return (folder / "UVMap_frente.exr").exists() and (folder / "UVMap_atras.exr").exists()

    def _frontback_zones(self, renders_path: Path, product: str) -> List[Dict]:
        """
        Zonas per-mesh del polera desde el ID unificado.

        El ID.png trae, por píxel, la superficie VISIBLE / N (0 = no imprimible,
        1/N = frente, 2/N = atras). Se recupera el índice (round(valor*N)) y se
        deriva la máscara de cada superficie (mask_k = id == k). Cada zona usa el
        UVMap del ángulo (UV de la superficie visible) -> composición per-mesh
        igual que el editor. No necesita re-UV.
        """
        id_path = renders_path / "ID.png"
        uv_path = renders_path / "UVMap.exr"
        if not id_path.exists() or not uv_path.exists():
            return []
        keys = list(self.PLAN_A_REGIONS.keys())
        n = len(keys)
        try:
            img = cv2.imread(str(id_path), cv2.IMREAD_UNCHANGED)
            if img is None:
                return []
            if img.ndim == 3:
                img = img[:, :, 0]
            idx = np.rint(img.astype(np.float32) / 255.0 * n).astype(np.int32)
            cache = self.python_app_path / "data" / "mask-cache" / product / renders_path.name
            cache.mkdir(parents=True, exist_ok=True)
            zones: List[Dict] = []
            for i, key in enumerate(keys, start=1):
                mask = (idx == i)
                mask_png = cache / f"ID_{key}.png"
                cv2.imwrite(str(mask_png), (mask.astype(np.uint8)) * 255)
                logger.info(f"🎭 Zona '{key}' (id={i}) desde ID.png: {int(mask.sum())} px")
                zones.append({"key": key, "uvmap": str(uv_path), "mask": str(mask_png)})
            return zones
        except Exception as e:
            logger.warning(f"⚠️ No se pudo derivar zonas de {id_path}: {e}")
            return []

    @staticmethod
    def _derive_zone_mask(master_path: Path, out_png: Path) -> Optional[Path]:
        """
        Deriva la MÁSCARA de una zona desde su master UV (UVMap_<key>.exr).

        El master se renderiza con SOLO ese mesh visible desde la misma cámara
        que el Beauty/ID del pack -> los píxeles con UV != 0 de ese master son
        exactamente la silueta de esa zona en el frame del pack (la unión de
        todas las zonas equivale al ID.png unificado).

        Es la SEÑAL CORRECTA para el pack de chapitas: como todas comparten la
        misma isla UV (mismo rango U/V), una región por bbox no discrimina, pero
        la silueta por píxel sí (Cylinder01..05 están en posiciones distintas).
        """
        try:
            uv = UVMapManager.load_uv_map_from_file(str(master_path), normalize=False)
            if uv is None:
                return None
            u = uv[:, :, 0]
            v = uv[:, :, 1]
            mask = (np.abs(u) > 1e-4) | (np.abs(v) > 1e-4)
            if not mask.any():
                logger.warning(f"⚠️ Master sin silueta UV: {master_path}")
                return None
            img = (mask.astype(np.uint8)) * 255
            # ID de zona en escala de grises (igual que ID.png de las otras zonas)
            out_png.parent.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(out_png), img)
            logger.info(f"🎭 Máscara de zona '{master_path.stem}' derivada: "
                        f"{int(mask.sum())} px -> {out_png.name}")
            return out_png
        except Exception as e:
            logger.error(f"❌ Error derivando máscara de {master_path}: {e}")
            return None

    def get_surface_regions(self, product: str) -> List[Dict]:
        """
        Detecta las superficies de un producto a partir de sus MASTERS UV
        (renders/<product>/UVMap_<key>.exr).

        - MULTIZONA por masters (pack de chapitas: UVMap_primera.exr ...):
          la señal NO es la región UV (todas las chapitas comparten el mismo
          rango U/V), sino la SILUETA por píxel del master (cada master se
          renderiza con solo su mesh visible desde la misma cámara del pack).
          Para cada zona se deriva su máscara ID_<key>.png y se devuelve el
          modelo {uvmap, mask}, el mismo que usa get_zones_for_product
          (composición MULTIZONA con u_shift de centrado). Producto-agnóstico:
          decide por los masters presentes, no por el nombre. La chapita
          individual (sin masters por zona) cae a la banda / máscara.
        - FRENTE/BACK clásico (polera): regiones plan A por master frente/atras.
        - Máscara (taza/gorra/chapita individual): sin masters por zona -> []
          y el caller usa la rama banda (ID.png unificado).
        """
        if product.startswith("taza_"):
            product_folder = self.python_app_path / "data" / "blender-passes" / "taza" / product
        else:
            product_folder = self.python_app_path / "data" / "blender-passes" / product

        if not product_folder.exists():
            logger.warning(f"⚠️ Carpeta de producto no existe para superficies: {product_folder}")
            return []

        # Masters por zona (excluye el UVMap unificado UVMap.exr).
        masters_by_zone: Dict[str, Path] = {}
        for master in sorted(product_folder.glob("UVMap_*.exr")):
            key = master.stem[len("UVMap_"):]
            if not key or key == "unified":
                continue
            masters_by_zone[key] = master

        extra_keys = [k for k in masters_by_zone if k not in ("frente", "atras")]
        if extra_keys:
            zones = []
            for key in extra_keys:
                master = masters_by_zone[key]
                mask_png = product_folder / f"ID_{key}.png"
                if not mask_png.exists():
                    derived = self._derive_zone_mask(master, mask_png)
                    if derived is None:
                        logger.warning(f"⚠️ No se pudo derivar máscara para zona '{key}'")
                        continue
                zones.append({"key": key, "uvmap": str(master), "mask": str(mask_png)})
                logger.info(f"🧩 Zona '{key}' | UVMap: {master.name} | máscara: {mask_png.name}")
            if zones:
                logger.info(f"🧩 Pack '{product}': {len(zones)} zonas por master")
                return zones

        if self._is_mask_based(product):
            logger.info(f"🧩 Producto {product}: composición por MÁSCARA (isla UV no plan A)")
            return []

        surfaces = []
        for key, region in self.PLAN_A_REGIONS.items():
            master = product_folder / f"UVMap_{key}.exr"
            if not master.exists():
                logger.warning(f"⚠️ Master no encontrado: {master}")
                continue
            surfaces.append({"key": key, "region": dict(region)})
            logger.info(f"🧩 Superficie '{key}' | region U[{region['umin']:.4f},{region['umax']:.4f}] "
                        f"V[{region['vmin']:.4f},{region['vmax']:.4f}]")

        if not surfaces:
            logger.warning(f"⚠️ No se detectaron superficies (masters) para {product}")
        return surfaces

    def _update_postgres(self, job_id: str, status: str, mockup_url: str = None):
        """Actualizar mockups y cart_items en PostgreSQL"""
        try:
            cursor = self.db_conn.cursor()
            
            if mockup_url:
                cursor.execute("""
                    UPDATE mockups 
                    SET status = %s, 
                        mockup_url = %s,
                        processed_at = NOW(),
                        updated_at = NOW()
                    WHERE job_id = %s
                """, (status, mockup_url, job_id))
            else:
                cursor.execute("""
                    UPDATE mockups 
                    SET status = %s,
                        processed_at = NOW(),
                        updated_at = NOW()
                    WHERE job_id = %s
                """, (status, job_id))
            
            if mockup_url and status == "completed":
                cursor.execute("""
                    UPDATE cart_items 
                    SET "mockupUrl" = %s,
                        updated_at = NOW()
                    WHERE customization->>'jobId' = %s
                    OR "mockupUrl" = %s
                """, (mockup_url, job_id, mockup_url))
                
                logger.info(f"✅ Cart items actualizados para job: {job_id}")
            
            self.db_conn.commit()
            logger.info(f"✅ PostgreSQL actualizado: {job_id} -> {status}")
            
        except Exception as e:
            logger.error(f"❌ Error actualizando PostgreSQL: {e}")
            self.db_conn.rollback()

    def _update_job_status(self, job_id: str, status: str, message: str = "", mockup_url: str = "", mockup_urls: List[str] = None):
        """Actualizar estado del job en Redis y PostgreSQL"""
        try:
            status_data = {
                "status": status,
                "message": message,
                "updatedAt": time.time()
            }
            
            if mockup_urls:
                status_data["mockupUrls"] = mockup_urls
                status_data["mockupUrl"] = mockup_urls[0] if mockup_urls else ""
            elif mockup_url:
                status_data["mockupUrl"] = mockup_url
                status_data["mockupUrls"] = [mockup_url]
            
            key = f"job:{job_id}"
            self.redis_client.setex(key, 24 * 3600, json.dumps(status_data))
            logger.info(f"📝 Redis actualizado: {job_id} -> {status}")
            
            final_url = mockup_url if mockup_url else (mockup_urls[0] if mockup_urls else None)
            self._update_postgres(job_id, status, final_url)
            
        except Exception as e:
            logger.error(f"❌ Error actualizando estado: {e}")

    def _r2_download(self, url: str, dest: Path) -> bool:
        """Descargar archivo desde R2 (o cualquier URL HTTP)"""
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            urllib.request.urlretrieve(url, str(dest))
            if dest.exists() and dest.stat().st_size > 0:
                logger.info(f"✅ Archivo descargado: {dest}")
                return True
            return False
        except Exception as e:
            logger.error(f"❌ Error descargando archivo: {e}")
            return False

    def _r2_upload(self, local_path: Path, key: str) -> bool:
        """Subir archivo a R2"""
        if not self.r2_enabled:
            return False
        try:
            self.s3_client.upload_file(str(local_path), self.r2_config["bucket"], key)
            logger.info(f"✅ Mockup subido a R2: {key}")
            return True
        except Exception as e:
            logger.error(f"❌ Error subiendo a R2: {e}")
            return False

    def _r2_public_url(self, key: str) -> str:
        return f"{self.r2_config['public_url'].rstrip('/')}/{key}"

    def _sync_renders_from_r2(self, product: str, renders: Dict = None, masters: List = None) -> bool:
        """
        Descarga los renders (Beauty/ID/UVMap por ángulo) y los MASTERS por
        superficie (UVMap_frente.exr / UVMap_atras.exr) hacia la ruta local que
        el worker ya espera (data/blender-passes/...). No cambia el pipeline:
        solo llena el disco local.

        Preferencia de origen:
          1. URLS firmadas que vienen en el job (`renders` = {angle: {...}} y
             `masters` = [{key, url}]). El bucket R2 no es público, así que las
             firmadas son la vía que funciona.
          2. Si no hay renders en el job, se leen los ángulos desde BD y se
             intenta con la URL pública (comportamiento anterior).

        Devuelve False si no hay ángulos disponibles para descargar.
        """
        # Los ángulos vienen definidos por el propio payload de renders (si existe).
        if renders:
            ok = True
            for angle, files in renders.items():
                angle_dir = self.get_render_path(product, angle)
                angle_dir.mkdir(parents=True, exist_ok=True)

                # No pisar renders locales existentes: si la carpeta ya tiene el
                # archivo, se respeta (los locales corregidos mandan sobre los firmados).
                def _sync(kind: str, local_name: str):
                    url = files.get(kind)
                    dest = angle_dir / local_name
                    if dest.exists():
                        logger.info(f"↩️  {local_name} ya existe local, se respeta (producto {product})")
                        return True
                    if not url:
                        return True
                    return self._r2_download(url, dest)

                if not _sync("beauty", "Beauty.png"):
                    ok = False
                if not _sync("id", "ID.png"):
                    ok = False
                if not _sync("uvmap", "UVMap.exr"):
                    ok = False

                # Multizona: máscaras por zona (ID_<zone>.png) vía URLs firmadas del job.
                # Formato A: files.masks = {zone: url}. Formato B: files.mask_<zone> = url.
                masks = files.get("masks") if isinstance(files, dict) else None
                if isinstance(masks, dict):
                    for zone, url in masks.items():
                        if not url:
                            continue
                        local_name = f"ID_{zone}.png"
                        dest = angle_dir / local_name
                        if not dest.exists() and self._r2_download(url, dest):
                            logger.info(f"   ✓ Máscara de zona: {local_name}")
                elif isinstance(files, dict):
                    for field_key, url in files.items():
                        if field_key.startswith("mask_") and url:
                            zone = field_key[len("mask_"):]
                            if not zone or (zone in ("beauty", "id", "uvmap")):
                                continue
                            local_name = f"ID_{zone}.png"
                            dest = angle_dir / local_name
                            if not dest.exists() and self._r2_download(url, dest):
                                logger.info(f"   ✓ Máscara de zona: {local_name}")

            if ok:
                logger.info(f"✅ Renders sincronizados (URLs firmadas del job) para {product}")
            else:
                logger.warning(f"⚠️ Algunos renders no se pudieron descargar (URLs firmadas) para {product}")

            # Masters UV por superficie (UVMap_frente/atras.exr) vía URLs firmadas.
            self._sync_masters(product, masters)
            return ok

        if not self.r2_enabled:
            logger.info("↩️  R2 no habilitado: usando renders locales")
            # Masters vía disco local (ya presentes) o descarta.
            return False

        # Leer ángulos del asset desde BD (los guarda el backend al crear)
        angles = []
        try:
            cursor = self.db_conn.cursor()
            cursor.execute("SELECT angles FROM assets_3d WHERE model_key = %s", (product,))
            row = cursor.fetchone()
            cursor.close()
            if row and row[0]:
                angles = list(row[0])
                logger.info(f"📐 Ángulos desde BD para {product}: {angles}")
            else:
                logger.warning(f"⚠️ No hay ángulos en BD para {product}")
                return False
        except Exception as e:
            logger.warning(f"⚠️ No se pudieron leer ángulos desde BD: {e}")
            return False

        if not angles:
            return False

        # Zonas multizona del asset (metadata.printZones) para bajar sus máscaras
        # (renders/<product>/<angle>/ID_<zone>.png) junto con Beauty/ID/UVMap.
        print_zones: list = []
        try:
            cursor = self.db_conn.cursor()
            cursor.execute("SELECT metadata FROM assets_3d WHERE model_key = %s", (product,))
            row = cursor.fetchone()
            cursor.close()
            meta = row[0] if row and row[0] else {}
            pz = (meta or {}).get("printZones") if isinstance(meta, dict) else None
            if isinstance(pz, list):
                print_zones = [z.get("key") if isinstance(z, dict) else z for z in pz]
        except Exception as e:
            logger.warning(f"⚠️ No se pudieron leer printZones desde BD: {e}")

        ok = True
        for angle in angles:
            angle_dir = self.get_render_path(product, angle)
            angle_dir.mkdir(parents=True, exist_ok=True)

            beauty_url = self._r2_public_url(f"renders/{product}/{angle}/Beauty.png")
            id_url = self._r2_public_url(f"renders/{product}/{angle}/ID.png")
            uvmap_url = self._r2_public_url(f"renders/{product}/{angle}/UVMap.exr")

            # No pisar renders locales existentes (los locales corregidos mandan).
            def _sync_public(url: str, local_name: str):
                dest = angle_dir / local_name
                if dest.exists():
                    logger.info(f"↩️  {local_name} ya existe local, se respeta (producto {product})")
                    return True
                return self._r2_download(url, dest)

            if not _sync_public(beauty_url, "Beauty.png"):
                ok = False
            if not _sync_public(id_url, "ID.png"):
                ok = False
            if not _sync_public(uvmap_url, "UVMap.exr"):
                ok = False

            # Multizona: bajar máscaras por zona (ID_<zone>.png)
            for zone in print_zones:
                if not zone:
                    continue
                mask_url = self._r2_public_url(f"renders/{product}/{angle}/ID_{zone}.png")
                _sync_public(mask_url, f"ID_{zone}.png")

        if ok:
            logger.info(f"✅ Renders sincronizados desde R2 para {product}")
        else:
            logger.warning(f"⚠️ Algunos renders no se pudieron descargar para {product}")

        # Masters por superficie en modo público (renders/<product>/UVMap_<key>.exr).
        self._sync_masters(product, masters)
        return ok

    def _sync_masters(self, product: str, masters: List = None):
        """
        Descarga los masters UV por superficie (renders/<product>/UVMap_<key>.exr)
        hacia data/blender-passes/<product>/UVMap_<key>.exr. En modo firmado usa
        las URLs del job (`masters` = [{key, url}]); en modo público usa la URL
        determinista del bucket.
        """
        if product.startswith("taza_"):
            product_folder = self.python_app_path / "data" / "blender-passes" / "taza" / product
        else:
            product_folder = self.python_app_path / "data" / "blender-passes" / product
        product_folder.mkdir(parents=True, exist_ok=True)

        if masters:
            for m in masters:
                key = m.get("key")
                url = m.get("url")
                if not key or not url:
                    continue
                dest = product_folder / f"UVMap_{key}.exr"
                if dest.exists():
                    logger.info(f"↩️  UVMap_{key}.exr ya existe local, se respeta")
                    continue
                if self._r2_download(url, dest):
                    logger.info(f"✅ Master sincronizado: UVMap_{key}.exr")
            return

        # Modo público (fallback): key determinista renders/<product>/UVMap_<key>.exr
        # Claves conocidas: frente/atras (polera) + las que vengan en
        # metadata.renders.masters (pack de chapitas: primera, segunda, ...).
        if not self.r2_enabled:
            return
        try:
            keys = ["frente", "atras"]
            cursor = self.db_conn.cursor()
            cursor.execute("SELECT metadata FROM assets_3d WHERE model_key = %s", (product,))
            row = cursor.fetchone()
            cursor.close()
            meta = row[0] if row and row[0] else {}
            rm = (meta or {}).get("renders", {}).get("masters") if isinstance(meta, dict) else None
            if isinstance(rm, dict):
                seen = set(keys)
                for k in rm:
                    if k and k not in seen:
                        keys.append(k)
                        seen.add(k)
            for key in keys:
                dest = product_folder / f"UVMap_{key}.exr"
                if dest.exists():
                    continue
                url = self._r2_public_url(f"renders/{product}/UVMap_{key}.exr")
                if self._r2_download(url, dest):
                    logger.info(f"✅ Master sincronizado (público): UVMap_{key}.exr")
        except Exception as e:
            logger.warning(f"⚠️ Error sincronizando masters (público): {e}")

    def _resolve_product_type(self, product: str) -> Optional[str]:
        """Resuelve el product_type del asset (assets_3d.product_type) desde PostgreSQL."""
        try:
            cursor = self.db_conn.cursor()
            cursor.execute("SELECT product_type FROM assets_3d WHERE model_key = %s", (product,))
            row = cursor.fetchone()
            cursor.close()
            return str(row[0]) if row and row[0] else None
        except Exception as e:
            logger.warning(f"⚠️ No se pudo resolver product_type para {product}: {e}")
            return None

    def _run_mockup_generator(self, product: str, angle: str, design_path: Path, output_dir: Path, output_filename: str, print_zone: Optional[Dict] = None, zones: Optional[List[Dict]] = None, design_back_path: Optional[Path] = None, zone_designs: Optional[Dict[str, Path]] = None) -> bool:
        """Ejecutar el generador de mockups para un ángulo específico"""
        try:
            # Construir ruta dinámica para este producto y ángulo
            renders_path = self.get_render_path(product, angle)
            
            if not renders_path.exists():
                logger.error(f"❌ Renders no encontrados en: {renders_path}")
                return False
            
            if not design_path.exists():
                logger.error(f"❌ Diseño no encontrado: {design_path}")
                return False
            
            beauty = renders_path / "Beauty.png"
            id_map = renders_path / "ID.png"
            
            if not beauty.exists() or not id_map.exists():
                logger.error(f"❌ Renders incompletos en: {renders_path}")
                logger.error(f"   Beauty.png existe: {beauty.exists()}")
                logger.error(f"   ID.png existe: {id_map.exists()}")
                return False
            
            output_dir.mkdir(parents=True, exist_ok=True)
            
            logger.info(f"📁 Design: {design_path.absolute()}")
            logger.info(f"📁 Renders: {renders_path.absolute()}")
            logger.info(f"   ✓ Beauty: {beauty.name}")
            logger.info(f"   ✓ ID: {id_map.name}")
            logger.info(f"📁 Output: {output_dir / output_filename}")

            main_script = self.python_app_path / "main.py"
            if not main_script.exists():
                main_script = self.python_app_path / "cli_main.py"
            
            if not main_script.exists():
                logger.error(f"❌ Script principal no encontrado")
                return False
            
            cmd = [
                sys.executable,
                str(main_script),
                "--product", product,
                "--product-type", self._resolve_product_type(product) or "",
                "--design", str(design_path.absolute()),
                "--render_passes", str(renders_path.absolute()),
                "--output", str(output_dir.absolute()),
                "--output-name", output_filename
            ]

            if design_back_path and design_back_path.exists():
                cmd.extend(["--design-back", str(design_back_path.absolute())])
                logger.info(f"   ✓ Design trasero: {design_back_path.name}")
            
            # Buscar UVMap específico para este ángulo
            uvmap_path = self.get_uvmap_path(renders_path)
            if uvmap_path:
                cmd.extend(["--uvmap", str(uvmap_path.absolute())])
                logger.info(f"   ✓ UVMap: {uvmap_path.name}")
            else:
                logger.warning(f"⚠️ UVMap no encontrado en {renders_path}")

            # Pasar zonas (multizona o superficies FRENTE/BACK) si las hay
            if zones:
                import json as _json
                # Pack multizona: cada zona lleva su propio diseño descargado
                # (designs por zona: primera, segunda, ... quinta). El modelo
                # heredado sin designs por zona sigue usando el diseño principal.
                zones_for_job = []
                for z in zones:
                    zz = dict(z)
                    z_key = z.get("key")
                    z_design = (zone_designs or {}).get(z_key)
                    if z_design and z_design.exists():
                        zz["design"] = str(z_design.absolute())
                    zones_for_job.append(zz)
                cmd.extend(["--zones", _json.dumps(zones_for_job)])
                logger.info(f"   ✓ Zonas ({len(zones_for_job)}): {[z['key'] for z in zones_for_job]}")
                if zone_designs:
                    logger.info(f"   ✓ {len(zone_designs)} diseños por zona: {list(zone_designs.keys())}")

            # Pasar print zone si el producto lo define en el job
            if print_zone:
                import json as _json
                cmd.extend(["--print-zone", _json.dumps(print_zone)])
                logger.info(f"   ✓ Print zone: {print_zone}")
            
            logger.info(f"🔄 Ejecutando comando:")
            logger.info(f"   {' '.join(cmd)}")
            
            result = subprocess.run(
                cmd,
                cwd=self.python_app_path,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=300
            )
            
            if result.stdout:
                logger.info(f"📄 STDOUT:\n{result.stdout}")
            if result.stderr:
                logger.error(f"📄 STDERR:\n{result.stderr}")
            
            logger.info(f"📄 Return code: {result.returncode}")
            
            if result.returncode != 0:
                logger.error(f"❌ Proceso terminó con error: {result.returncode}")
                return False
            
            output_file = output_dir / output_filename
            if not output_file.exists():
                logger.error(f"❌ Archivo no generado: {output_file}")
                return False
            
            logger.info(f"✅ Mockup generado exitosamente: {output_file}")
            return True
            
        except subprocess.TimeoutExpired:
            logger.error("⏰ Timeout en generador de mockups")
            return False
        except Exception as e:
            logger.error(f"❌ Error ejecutando generador: {e}", exc_info=True)
            return False

    def process_job(self, job_data: Dict) -> bool:
        """Procesar un job de mockup"""
        job_id = job_data.get("jobId")
        product = job_data.get("product")
        design_key = job_data.get("designKey")
        user_id = job_data.get("userId", "anonymous")
        
        if not all([job_id, product, design_key]):
            logger.error("❌ Job inválido: faltan campos requeridos")
            return False
        
        try:
            logger.info("="*60)
            logger.info(f"🎯 INICIANDO JOB: {job_id}")
            logger.info(f"   Producto: {product}")
            logger.info(f"   Design key: {design_key}")
            logger.info(f"   User ID: {user_id}")
            logger.info("="*60)
            
            self._update_job_status(job_id, "processing", "Generando mockups...")

            design_key_normalized = design_key.replace('\\', '/')

            # Si el job trae un URL firmado del diseño, usarlo directamente
            design_url = job_data.get("designUrl") or job_data.get("design_url")

            # Si design_key es una URL, descargarla desde R2
            if design_key_normalized.startswith(("http://", "https://")) or design_url:
                download_dir = self.python_app_path / "data" / "downloads"
                if design_url:
                    design_key_normalized = design_url
                suffix = Path(design_key.replace('\\', '/')).suffix or ".png"
                design_path = download_dir / f"design_{job_id}{suffix}"
                logger.info(f"⬇️ Descargando diseño desde R2: {design_key_normalized[:120]}")
                if not self._r2_download(design_key_normalized, design_path):
                    error_msg = f"No se pudo descargar el diseño desde R2: {design_key}"
                    logger.error(f"❌ {error_msg}")
                    self._update_job_status(job_id, "failed", error_msg)
                    return False
            else:
                # Si hay R2 configurado, el designKey es una clave dentro del bucket
                # → descargarlo usando la URL pública
                if self.r2_enabled:
                    download_dir = self.python_app_path / "data" / "downloads"
                    suffix = Path(design_key_normalized).suffix or ".png"
                    design_path = download_dir / f"design_{job_id}{suffix}"
                    design_url = f"{self.r2_config['public_url'].rstrip('/')}/{design_key_normalized}"
                    logger.info(f"⬇️ Descargando diseño desde R2 (por key): {design_url}")
                    if not self._r2_download(design_url, design_path):
                        error_msg = f"No se pudo descargar el diseño desde R2: {design_key}"
                        logger.error(f"❌ {error_msg}")
                        self._update_job_status(job_id, "failed", error_msg)
                        return False
                else:
                    design_path = self.base_storage_path / design_key_normalized

            if not design_path.exists():
                error_msg = f"Diseño no encontrado: {design_key}"
                logger.error(f"❌ {error_msg}")
                self._update_job_status(job_id, "failed", error_msg)
                return False

            # --- Diseño de la superficie ATRÁS (opcional) ---
            design_back_path = None
            design_back_key = job_data.get("designBackKey")
            design_back_url = job_data.get("designBackUrl") or job_data.get("design_back_url")
            if design_back_key or design_back_url:
                dl_dir = self.python_app_path / "data" / "downloads"
                dl_dir.mkdir(parents=True, exist_ok=True)
                suffix = Path((design_back_key or "back.png").replace('\\', '/')).suffix or ".png"
                design_back_path = dl_dir / f"design_back_{job_id}{suffix}"

                if design_back_url:
                    src = design_back_url
                elif design_back_key.startswith(("http://", "https://")):
                    src = design_back_key
                elif self.r2_enabled:
                    src = f"{self.r2_config['public_url'].rstrip('/')}/{design_back_key}"
                else:
                    src = self.base_storage_path / design_back_key
                    if not Path(src).exists():
                        error_msg = f"Diseño trasero no encontrado: {design_back_key}"
                        logger.error(f"❌ {error_msg}")
                        self._update_job_status(job_id, "failed", error_msg)
                        return False

                logger.info(f"⬇️ Descargando diseño trasero: {str(src)[:120]}")
                if self._r2_download(str(src), design_back_path):
                    logger.info(f"✅ Diseño trasero descargado: {design_back_path}")
                else:
                    error_msg = f"No se pudo descargar el diseño trasero: {design_back_key}"
                    logger.error(f"❌ {error_msg}")
                    self._update_job_status(job_id, "failed", error_msg)
                    return False

            output_dir = self.python_app_path / "data" / "output"
            output_dir.mkdir(parents=True, exist_ok=True)

            # --- Diseños por zona de impresión (pack: primera..quinta) ---
            zone_designs: Dict[str, Path] = {}
            zone_designs_data = job_data.get("zoneDesigns") or job_data.get("zone_designs")
            if zone_designs_data:
                dl_dir = self.python_app_path / "data" / "downloads"
                dl_dir.mkdir(parents=True, exist_ok=True)
                for zd in zone_designs_data:
                    z_key = zd.get("key")
                    z_design_key = zd.get("designKey") or zd.get("design_key")
                    z_design_url = zd.get("designUrl") or zd.get("design_url")
                    if not z_key or not (z_design_key or z_design_url):
                        continue
                    suffix = Path((z_design_key or "zone.png").replace('\\', '/')).suffix or ".png"
                    z_path = dl_dir / f"design_{job_id}_{z_key}{suffix}"
                    if z_design_url:
                        src = z_design_url
                    elif z_design_key.startswith(("http://", "https://")):
                        src = z_design_key
                    elif self.r2_enabled:
                        src = f"{self.r2_config['public_url'].rstrip('/')}/{z_design_key}"
                    else:
                        src = self.base_storage_path / z_design_key
                        if not Path(src).exists():
                            logger.error(f"❌ Diseño de zona '{z_key}' no encontrado: {z_design_key}")
                            self._update_job_status(job_id, "failed", f"Diseño de zona '{z_key}' no encontrado")
                            return False
                    logger.info(f"⬇️ Descargando diseño de zona '{z_key}': {str(src)[:120]}")
                    if self._r2_download(str(src), z_path):
                        zone_designs[z_key] = z_path
                        logger.info(f"✅ Diseño de zona '{z_key}' descargado: {z_path.name}")
                    else:
                        error_msg = f"No se pudo descargar el diseño de zona '{z_key}'"
                        logger.error(f"❌ {error_msg}")
                        self._update_job_status(job_id, "failed", error_msg)
                        return False
                if zone_designs:
                    logger.info(f"✅ Diseños por zona listos: {list(zone_designs.keys())}")

            # Sincronizar renders desde R2 (si el job trae URLs firmadas, esas mandan).
            # Si no baja nada, se mantiene el comportamiento actual: disco local.
            self._sync_renders_from_r2(product, job_data.get("renders") or None, job_data.get("masters") or None)

            # Detectar ángulos disponibles para este producto
            angles = self.get_angles_for_product(product)
            
            if not angles:
                error_msg = f"No se encontraron renders para el producto: {product}"
                logger.error(f"❌ {error_msg}")
                self._update_job_status(job_id, "failed", error_msg)
                return False
            
            logger.info(f"📐 Ángulos a generar: {len(angles)}")
            for angle in angles:
                logger.info(f"   - {angle}")
            
            # Guardar ángulos en PostgreSQL
            try:
                cursor = self.db_conn.cursor()
                cursor.execute("""
                    UPDATE assets_3d 
                    SET angles = %s, updated_at = NOW()
                    WHERE model_key = %s
                """, (angles, product))
                self.db_conn.commit()
                logger.info(f"✅ Ángulos guardados en BD para {product}: {angles}")
            except Exception as e:
                logger.warning(f"⚠️ No se pudieron guardar ángulos en BD: {e}")
            
            mockup_urls = []
            failed_angles = []

            # Print zone del producto (opcional) para recortar el diseño a la superficie real
            print_zone = job_data.get("printZone") or job_data.get("metadata", {}).get("printZone")

            # Zonas de composición.
            # - PER-MESH (polera frente/atrás): las máscaras y el UVMap son POR
            #   ÁNGULO (la superficie visible cambia de una toma a otra), así que
            #   se calculan dentro del loop de ángulos.
            # - Región UV (PLAN_A) / multizona por masters: no dependen del
            #   ángulo; se calculan una vez y se reusan.
            design_back_active = design_back_key is not None or design_back_url is not None
            frontback = self._has_frontback_masters(product)
            zones = []
            if not frontback:
                surface_masters = self.get_surface_regions(product)
                if surface_masters:
                    zones = surface_masters
                    logger.info(f"👇 Producto {product}: {len(zones)} superficies (FRENTE/BACK por región UV)")
                else:
                    for angle in angles:
                        probes = self.get_render_path(product, angle)
                        zones = self.get_zones_for_product(probes, product)
                        if zones:
                            break
                    if zones:
                        logger.info(f"👇 Producto {product}: {len(zones)} zonas de impresión")

            # Modo BLENDER (render por partes): re-renderiza los ángulos con el
            # diseño horneado como textura -> idéntico al preview GLB. Si no se
            # puede usar Blender, cae al compositor.
            render_by_angle = None
            if self.render_mode == "blender" and design_back_active and design_path.exists():
                render_by_angle = render_frontback_designs(design_path, design_back_path, output_dir)
                if render_by_angle:
                    logger.info(f"🎬 Modo BLENDER FRENTE/BACK: {len(render_by_angle)} ángulos re-renderizados")
                else:
                    logger.warning("➡️ Blender no disponible; usando compositor")

            for index, angle in enumerate(angles, 1):
                try:
                    logger.info(f"🔄 [{index}/{len(angles)}] Generando ángulo: {angle}")

                    output_filename = f"mockup_{job_id}_{product}_{angle}.png"

                    # Per-mesh: zonas del ángulo actual (máscaras + UVMap propios).
                    angle_zones = zones
                    if frontback:
                        probes = self.get_render_path(product, angle)
                        angle_zones = self._frontback_zones(probes, product)
                        if angle_zones:
                            logger.info(f"🧩 {angle}: {len(angle_zones)} zonas per-mesh")
                        else:
                            angle_zones = self.get_surface_regions(product)
                            logger.warning(f"⚠️ {angle}: sin zonas per-mesh; fallback por región")

                    if render_by_angle and angle in render_by_angle:
                        # Re-render con diseño horneado (textura real, misma que el GLB)
                        mockup_file = output_dir / output_filename
                        shutil.copy2(render_by_angle[angle], mockup_file)
                        success = mockup_file.exists()
                        if success:
                            logger.info(f"✅ [{index}/{len(angles)}] Mockup (Blender): {angle}")
                    else:
                        success = self._run_mockup_generator(product, angle, design_path, output_dir, output_filename, print_zone=print_zone, zones=angle_zones, design_back_path=design_back_path, zone_designs=zone_designs)
                    
                    if success:
                        mockup_file = output_dir / output_filename
                        
                        if mockup_file.exists():
                            mockup_dest_name = f"mockup_{job_id}_{product}_{angle}.png"
                            mockup_key = f"outputs/{mockup_dest_name}"
                            
                            if self.r2_enabled:
                                if self._r2_upload(mockup_file, mockup_key):
                                    mockup_urls.append(self._r2_public_url(mockup_key))
                                    logger.info(f"✅ [{index}/{len(angles)}] Mockup subido a R2: {angle}")
                                else:
                                    failed_angles.append(angle)
                                    logger.error(f"❌ [{index}/{len(angles)}] Error subiendo a R2: {angle}")
                            else:
                                mockup_dest = self.base_storage_path / "outputs" / mockup_dest_name
                                mockup_dest.parent.mkdir(parents=True, exist_ok=True)
                                
                                shutil.copy2(mockup_file, mockup_dest)
                                logger.info(f"✅ [{index}/{len(angles)}] Mockup copiado: {angle}")
                                
                                mockup_urls.append(mockup_key)
                            
                            progress_msg = f"Generados {len(mockup_urls)}/{len(angles)} ángulos"
                            self._update_job_status(job_id, "processing", progress_msg, mockup_urls=mockup_urls)
                        else:
                            failed_angles.append(angle)
                            logger.error(f"❌ [{index}/{len(angles)}] Archivo no encontrado: {angle}")
                    else:
                        failed_angles.append(angle)
                        logger.error(f"❌ [{index}/{len(angles)}] Error generando: {angle}")
                        
                except Exception as e:
                    failed_angles.append(angle)
                    logger.error(f"❌ [{index}/{len(angles)}] Excepción en {angle}: {e}", exc_info=True)
            
            if len(mockup_urls) > 0:
                logger.info("="*60)
                logger.info(f"✅ JOB COMPLETADO: {job_id}")
                logger.info(f"   Ángulos exitosos: {len(mockup_urls)}/{len(angles)}")
                if failed_angles:
                    logger.warning(f"   Ángulos fallidos: {', '.join(failed_angles)}")
                logger.info("="*60)
                
                mockup_url = f"{self.render_url}/api/mockup/{job_id}/download"
                
                self._update_job_status(
                    job_id,
                    "completed",
                    f"Mockups generados: {len(mockup_urls)}/{len(angles)} ángulos",
                    mockup_url=mockup_url,
                    mockup_urls=mockup_urls
                )
                return True
            else:
                error_msg = f"Todos los ángulos fallaron: {', '.join(failed_angles)}"
                logger.error(f"❌ {error_msg}")
                self._update_job_status(job_id, "failed", error_msg)
                return False

        except Exception as e:
            error_msg = f"Error procesando job: {str(e)}"
            logger.error(f"❌ {error_msg}", exc_info=True)
            self._update_job_status(job_id, "failed", error_msg)
            return False

    def run(self):
        """Ejecutar worker en loop infinito"""
        logger.info("="*60)
        logger.info("🔄 WORKER INICIADO (MODO DINÁMICO)")
        logger.info(f"🔄 Cola Redis: mockup:jobs")
        logger.info("="*60)
        
        while True:
            try:
                result = self.redis_client.brpop("mockup:jobs", timeout=30)
                
                if result:
                    _, job_json = result
                    logger.info(f"📨 Job recibido desde Redis")
                    logger.debug(f"   JSON: {job_json}")
                    
                    try:
                        job_data = json.loads(job_json)
                        self.process_job(job_data)
                    except json.JSONDecodeError as e:
                        logger.error(f"❌ JSON inválido: {e}")
                else:
                    logger.debug("⏰ Timeout esperando jobs (normal)")
                    if not self.redis_client.ping():
                        logger.warning("🫀 Reconectando a Redis...")
                        self.redis_client = redis.Redis.from_url(
                            self.redis_url,
                            decode_responses=True
                        )
                        
            except KeyboardInterrupt:
                logger.info("🛑 Worker detenido por usuario (Ctrl+C)")
                break
            except redis.ConnectionError as e:
                logger.error(f"💥 Error de conexión Redis: {e}")
                logger.info("⏳ Reintentando en 5 segundos...")
                time.sleep(5)
            except Exception as e:
                logger.error(f"💥 Error en loop principal: {e}", exc_info=True)
                time.sleep(1)

def test_job():
    """Función de prueba"""
    try:
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
        redis_client = redis.Redis.from_url(redis_url, decode_responses=True)
        
        test_job = {
            "jobId": f"test_{int(time.time())}",
            "product": "taza_azul",
            "designKey": "designs/test_logo.png",
            "userId": "test_user"
        }
        
        redis_client.lpush("mockup:jobs", json.dumps(test_job))
        logger.info(f"✅ Job de prueba enviado: {test_job['jobId']}")
        
    except Exception as e:
        logger.error(f"❌ Error enviando job de prueba: {e}")

def main():
    """Punto de entrada principal"""
    import argparse
    
    parser = argparse.ArgumentParser(description='Worker de generación de mockups')
    parser.add_argument('--test', action='store_true', help='Enviar un job de prueba')
    args = parser.parse_args()
    
    try:
        if args.test:
            test_job()
            return
        
        worker = MockupWorker()
        worker.run()
        
    except KeyboardInterrupt:
        logger.info("🛑 Worker detenido por usuario")
    except Exception as e:
        logger.error(f"💥 Error iniciando worker: {e}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    main()