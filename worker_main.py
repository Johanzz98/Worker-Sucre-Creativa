import redis
import json
import base64
import logging
import time
import sys
import subprocess
import os
import shutil
import hashlib
import psycopg2
import urllib.parse
import urllib.request
import urllib.error
import http.server
import socketserver
import threading
import numpy as np
import cv2
from concurrent.futures import ThreadPoolExecutor, as_completed
from psycopg2.extras import RealDictCursor
from pathlib import Path
from typing import Dict, List, Optional
from dotenv import load_dotenv

from app.core.uv_manager import UVMapManager
from app.application.render_frontback import render_frontback_designs
from app.application.generate_mockup import generate_mockup as _generate_mockup

# boto3 opcional: solo necesario si se usa R2 para subir los mockups
try:
    import boto3
    from botocore.config import Config as BotoConfig
    BOTO3_AVAILABLE = True
except ImportError:
    BOTO3_AVAILABLE = False

# Cargar variables de entorno
load_dotenv()

# Keepalive TCP para PostgreSQL: sin esto, Supabase/el pooler cierra la conexión
# tras un rato idle y el primer job del día falla todos los UPDATE.
DB_KEEPALIVE = {
    "keepalives": 1,
    "keepalives_idle": 60,
    "keepalives_interval": 15,
    "keepalives_count": 3,
}

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

# Reducir ruido de librerías (redis-py 8 genera mucho debug por maintenance notifications)
try:
    logging.getLogger("redis").setLevel(logging.WARNING)
    logging.getLogger("redis.connection").setLevel(logging.WARNING)
    logging.getLogger("redis.client").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("boto3").setLevel(logging.WARNING)
    # s3transfer/botocore en DEBUG escriben ~40 líneas por subida (ruido puro en
    # el log del worker).
    logging.getLogger("s3transfer").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
except Exception:
    pass

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
        with socketserver.ThreadingTCPServer(("", RENDER_PORT), CustomHandler) as httpd:
            # La API viva (render on-demand) pide varios archivos a la vez; con
            # el TCPServer de un solo hilo las descargas se serializan.
            httpd.daemon_threads = True
            logger.info(f"📁 Servidor de renders corriendo en http://localhost:{RENDER_PORT} (threaded)")
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

# Lock de instancia única: con dos workers, los jobs se reparten entre ambos y
# cada uno arranca con la caché de passes en frío (se veía como jobs de 7 s).
_INSTANCE_LOCK_PORT = 47654
_instance_lock_socket = None


def acquire_instance_lock() -> bool:
    """Devuelve True si este proceso es el único worker. El socket se mantiene
    abierto durante toda la vida del proceso (se libera solo al salir)."""
    global _instance_lock_socket
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", _INSTANCE_LOCK_PORT))
        s.listen(1)
    except OSError:
        s.close()
        return False
    _instance_lock_socket = s
    return True

class MockupWorker:
    def _create_redis_client(self):
        return redis.Redis.from_url(
            self.redis_url,
            decode_responses=True,
            socket_connect_timeout=10,
            socket_timeout=self.socket_timeout,
            socket_keepalive=True,
        )

    def __init__(self, redis_url: str = None):
        # Conexión a Redis (usar REDIS_URL si está disponible)
        redis_url = redis_url or os.getenv("REDIS_URL", "redis://localhost:6379")
        self.redis_url = redis_url
        self.brpop_timeout = 30
        self.socket_timeout = 40
        try:
            self.redis_client = self._create_redis_client()
            self.redis_client.ping()
            logger.info(f"✅ Conectado a Redis: {redis_url}")
        except Exception as e:
            logger.error(f"❌ Error conectando a Redis: {e}")
            raise
        
        # PostgreSQL: el worker puede atender jobs de desarrollo (BD local) y
        # de producción (Neon) en la misma cola de Redis. Cada job trae su
        # `dbTarget`; si es antiguo y no lo trae, se detecta buscando su fila
        # en `mockups` en ambas bases.
        legacy_db_url = os.getenv("DATABASE_URL")
        local_db_url = os.getenv("DATABASE_URL_LOCAL")
        neon_db_url = os.getenv("DATABASE_URL_NEON")

        def _looks_local(url: Optional[str]) -> bool:
            return bool(url) and ("localhost" in url or "127.0.0.1" in url)

        if not local_db_url and _looks_local(legacy_db_url):
            local_db_url = legacy_db_url
        if not neon_db_url and legacy_db_url and not _looks_local(legacy_db_url):
            neon_db_url = legacy_db_url
        if not local_db_url and not neon_db_url:
            # Compatibilidad con la configuración antigua por DB_HOST/DB_PORT/...
            db_host = os.getenv("DB_HOST")
            db_port = os.getenv("DB_PORT")
            db_name = os.getenv("DB_NAME")
            db_user = os.getenv("DB_USER")
            db_password = os.getenv("DB_PASSWORD")
            if all([db_host, db_port, db_name, db_user, db_password is not None]):
                escaped_password = urllib.parse.quote_plus(db_password)
                local_db_url = f"postgresql://{db_user}:{escaped_password}@{db_host}:{db_port}/{db_name}"

        self.db_urls: Dict[str, str] = {}
        if local_db_url:
            self.db_urls["local"] = local_db_url
        if neon_db_url:
            self.db_urls["neon"] = neon_db_url

        if not self.db_urls:
            raise RuntimeError("❌ No hay DATABASE_URL_LOCAL ni DATABASE_URL_NEON configuradas")

        requested_target = os.getenv("DB_TARGET")
        self._default_db_target = (
            requested_target
            if requested_target in self.db_urls
            else ("local" if "local" in self.db_urls else "neon")
        )
        self.db_target: Optional[str] = None
        self.db_url: Optional[str] = None
        self.db_conn = None
        if not self._switch_db(self._default_db_target):
            raise RuntimeError(f"❌ No se pudo conectar a PostgreSQL ({self._default_db_target})")
        logger.info(
            f"🗄️ BD configuradas: {', '.join(self.db_urls.keys())} | "
            f"por defecto: {self._default_db_target}"
        )
        
        # Configuración R2 (opcional: subir mockups a Cloudflare R2)
        self.r2_config = {
            "endpoint": os.getenv("R2_ENDPOINT"),
            "access_key_id": os.getenv("R2_ACCESS_KEY_ID"),
            "access_key_secret": os.getenv("R2_ACCESS_KEY_SECRET"),
            "bucket": os.getenv("R2_BUCKET"),
            "private_bucket": os.getenv("R2_PRIVATE_BUCKET"),
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
                # Timeouts acotados: sin esto, un socket keep-alive viejo hacia
                # Cloudflare puede colgar un PUT ~24 s (visto en producción)
                # antes de fallar. Con esto el peor caso es 3 intentos cortos.
                config=BotoConfig(
                    connect_timeout=5,
                    # 30 s hacía que un PUT atascado se quedara 10-12 s sin
                    # reintentarse. Con 12 s se reintenta antes. El timeout es
                    # por lectura, así que un envío lento pero que progresa
                    # (subida lenta) no se corta.
                    read_timeout=12,
                    max_pool_connections=16,
                    retries={"max_attempts": 3, "mode": "standard"},
                    tcp_keepalive=True,
                ),
            )
            logger.info("✅ R2 storage habilitado para subir mockups")
        else:
            self.s3_client = None
            if BOTO3_AVAILABLE:
                logger.warning("⚠️ R2 no configurado (faltan variables o bucket), usando almacenamiento local")
            else:
                logger.warning("⚠️ boto3 no instalado, R2 deshabilitado (pip install boto3)")

        # Salida de los mockups: 1024 px + JPEG q97. Es una miniatura de
        # carrito/pedido (el print usa el diseño original, no el mockup), así
        # que 1024 se ve limpio y pesa ~500 KB en vez de 5,5 MB. La calidad 97
        # reduce el ringing del JPEG alrededor de los blancos del diseño (las
        # "motas" claras): en un diseño con mucho blanco baja de 52 a 35 motas
        # de 1-9 px frente a q95, y a q98 baja a 18. MOCKUP_OUTPUT_SIZE y
        # MOCKUP_JPEG_QUALITY se pueden cambiar por .env sin tocar código.
        os.environ.setdefault("MOCKUP_OUTPUT_SIZE", "1024")
        os.environ.setdefault("MOCKUP_JPEG_QUALITY", "97")

        # Cache de product_type (1 consulta por producto en vez de 4-5 por job).
        self._product_type_cache: Dict[str, Optional[str]] = {}
        # (Las claves incluyen el target de BD: local/neon)

        # Si el producto se sincronizó hace < N segundos y los passes están en
        # disco, no se revalidan los 12 ETags contra R2 (~0,5 s por job). Un
        # re-render desde el dashboard tarda mucho más que esta ventana.
        self._last_sync: Dict[str, float] = {}
        self._sync_ttl = float(os.getenv("MOCKUP_SYNC_TTL", "120"))

        # Diseños ya descargados hace poco: evita la revalidación ETag (~0,2 s)
        # cuando el mismo diseño se reusa en jobs seguidos.
        self._last_design: Dict[str, float] = {}

        # Últimos ángulos escritos en BD por producto (evita un UPDATE por job).
        self._angles_cache: Dict[str, List[str]] = {}

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

        # Orden estable con la vista principal PRIMERO: el frontend usa
        # mockupUrls[0] como miniatura del carrito y el backend conserva solo
        # ese ángulo al limpiar un pedido. Antes se usaba el orden arbitrario
        # de iterdir() y muchas veces quedaba "atras" como imagen principal.
        _priority = {
            "centro": 0, "frente": 0, "frontal": 0, "front": 0, "principal": 0,
            "izquierda": 1, "derecha": 2,
            "atras": 9, "trasera": 9, "back": 9,
        }
        angles.sort(key=lambda a: (_priority.get(a.lower(), 5), a))

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

    def _id_has_sections(self, renders_path: Path) -> bool:
        """True si el ID.png trae >=2 valores distintos (>0), o sea un ID por
        sección. NO escribe nada: sirve para decidir si trabajamos por ID (pack)
        y evitar derivar las máscaras ID_<key>.png de los masters."""
        id_path = renders_path / "ID.png"
        if not id_path.exists():
            return False
        try:
            img = cv2.imread(str(id_path), cv2.IMREAD_UNCHANGED)
            if img is None:
                return False
            if img.ndim == 3:
                img = img[:, :, 0]
            return len({int(v) for v in np.unique(img) if int(v) > 0}) >= 2
        except Exception as e:
            logger.warning(f"⚠️ No se pudo leer {id_path}: {e}")
            return False

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
            coverage = float(mask.mean())
            if coverage > 0.5:
                logger.error(
                    f"❌ Master '{master_path.name}': la silueta cubre "
                    f"{coverage * 100:.1f}% del frame (deberia ser solo su zona). "
                    f"Master invalido (fondo/otra malla con UV se colo). Se "
                    f"IGNORA esta zona -> re-renderiza el master."
                )
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
                # La máscara debe ser la silueta de ESA zona. Si ya existe pero
                # cubre casi todo el frame, es basura de una corrida previa con
                # un master malo (UV en todo el frame) -> se re-deriva y valida.
                need_derive = not mask_png.exists()
                if mask_png.exists():
                    _m = cv2.imread(str(mask_png), cv2.IMREAD_GRAYSCALE)
                    if _m is None or (_m > 127).mean() > 0.5:
                        need_derive = True
                if need_derive:
                    if self._derive_zone_mask(master, mask_png) is None:
                        logger.warning(f"⚠️ Zona '{key}' IGNORADA (máscara inválida)")
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

    def _apply_pg_update(self, conn, job_id: str, status: str, mockup_url: str = None):
        """Ejecuta el UPDATE de mockups/cart_items sobre una conexión dada."""
        cursor = conn.cursor()

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

        conn.commit()
        logger.info(f"✅ PostgreSQL actualizado: {job_id} -> {status}")

    def _update_postgres(self, job_id: str, status: str, mockup_url: str = None,
                         db_target: Optional[str] = None, isolated: bool = False):
        """Actualizar mockups y cart_items en la BD correcta.

        - `isolated=True`: abre una conexión propia (se usa en el hilo de
          persistencia para que el job siguiente no cambie la conexión).
        - `db_target`: 'local' o 'neon'. Si no viene, se usa la activa.
        """
        target = db_target if db_target in self.db_urls else self.db_target
        if target not in self.db_urls:
            target = self._default_db_target

        if isolated:
            for _attempt in (0, 1):
                conn = None
                try:
                    conn = psycopg2.connect(self.db_urls[target], connect_timeout=5, **DB_KEEPALIVE)
                    self._apply_pg_update(conn, job_id, status, mockup_url)
                    return
                except Exception as e:
                    logger.error(f"❌ Error actualizando PostgreSQL ({target}): {e}")
                finally:
                    if conn is not None:
                        try:
                            conn.close()
                        except Exception:
                            pass
            return

        for attempt in (0, 1):
            try:
                self._apply_pg_update(self.db_conn, job_id, status, mockup_url)
                return

            except Exception as e:
                logger.error(f"❌ Error actualizando PostgreSQL ({target}): {e}")
                try:
                    self.db_conn.rollback()
                except Exception:
                    pass
                if attempt == 0:
                    # Conexión muerta (Neon cierra idle): reconectar y
                    # reintentar una vez para no perder el estado final.
                    self._reconnect_db()
                else:
                    return

    def _reconnect_db(self):
        """Reconecta PostgreSQL si la conexión se cerró por inactividad."""
        try:
            try:
                if self.db_conn is not None:
                    self.db_conn.close()
            except Exception:
                pass
            self.db_conn = psycopg2.connect(self.db_url, connect_timeout=5, **DB_KEEPALIVE)
            logger.info(f"🔄 Reconectado a PostgreSQL ({self.db_target})")
        except Exception as e:
            logger.error(f"❌ No se pudo reconectar a PostgreSQL: {e}")

    def _switch_db(self, target: str, force: bool = False) -> bool:
        """Cambia la conexión activa al target indicado ('local' o 'neon')."""
        target = target if target in self.db_urls else self._default_db_target
        if not force and self.db_conn is not None and self.db_target == target:
            return True

        try:
            if self.db_conn is not None:
                try:
                    self.db_conn.close()
                except Exception:
                    pass
            self.db_conn = psycopg2.connect(self.db_urls[target], connect_timeout=5, **DB_KEEPALIVE)
            self.db_target = target
            self.db_url = self.db_urls[target]
            logger.info(f"🗄️ PostgreSQL activo: {target}")
            return True
        except Exception as e:
            logger.error(f"❌ Error conectando a PostgreSQL ({target}): {e}")
            self.db_conn = None
            self.db_target = target
            self.db_url = self.db_urls.get(target)
            return False

    def _detect_job_db(self, job_id: str) -> Optional[str]:
        """Busca en qué BD existe la fila del job (para jobs antiguos sin dbTarget)."""
        for target, url in self.db_urls.items():
            conn = None
            try:
                conn = psycopg2.connect(url, connect_timeout=3, **DB_KEEPALIVE)
                cursor = conn.cursor()
                cursor.execute("SELECT 1 FROM mockups WHERE job_id = %s LIMIT 1", (job_id,))
                found = cursor.fetchone() is not None
                cursor.close()
                if found:
                    logger.info(f"🔎 Job {job_id} encontrado en BD {target}")
                    return target
            except Exception as e:
                logger.warning(f"⚠️ No se pudo consultar BD {target}: {e}")
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
        return None

    def _update_job_status(self, job_id: str, status: str, message: str = "", mockup_url: str = "", mockup_urls: List[str] = None, persist: bool = True, ttl: Optional[int] = None):
        """Actualizar estado del job en Redis y (opcionalmente) PostgreSQL.

        `ttl` permite que los previews efímeros expiren rápido de Redis en vez
        de quedarse 24 h (el estado incluye las imágenes en base64).
        """
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
            self.redis_client.setex(key, ttl or (24 * 3600), json.dumps(status_data))
            logger.info(f"📝 Redis actualizado: {job_id} -> {status}")
            
            final_url = mockup_url if mockup_url else (mockup_urls[0] if mockup_urls else None)
            if persist:
                self._update_postgres(job_id, status, final_url)
            
        except Exception as e:
            logger.error(f"❌ Error actualizando estado: {e}")

    def _r2_download(self, url: str, dest: Path, use_cache: bool = True) -> bool:
        """Descargar archivo desde R2 (o cualquier URL HTTP).

        Descarga a un .tmp y reemplaza de forma atómica: si la descarga falla,
        NO pisa el archivo bueno que ya estaba en disco.

        Con `use_cache` manda ``If-None-Match`` con el ETag guardado junto al
        archivo: los passes (Beauty/ID/UVMap) son estáticos entre jobs, así que
        un 304 evita bajar ~12 archivos en cada pedido."""
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            etag_file = dest.with_name(dest.name + ".etag")
            etag = None
            if use_cache and dest.exists() and etag_file.exists():
                etag = etag_file.read_text(encoding="utf-8").strip() or None

            req = urllib.request.Request(url)
            if etag:
                req.add_header("If-None-Match", etag)

            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = resp.read()
                    new_etag = resp.headers.get("ETag")
            except urllib.error.HTTPError as e:
                if e.code == 304:
                    # El archivo local ya es la versión vigente.
                    return True
                raise

            if not data:
                return False

            tmp = dest.with_name(dest.name + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(dest)
            if new_etag:
                etag_file.write_text(new_etag, encoding="utf-8")
            elif etag_file.exists():
                etag_file.unlink()
            logger.info(f"✅ Archivo descargado: {dest}")
            return True
        except Exception as e:
            logger.error(f"❌ Error descargando archivo: {e}")
            return False

    def _download_many(self, items, workers: int = 8) -> bool:
        """Descarga en paralelo una lista de (url, destino). Devuelve True si
        todas salieron bien. En serie, los 12 passes tardaban ~3,4 s por job."""
        items = [(u, d) for u, d in items if u]
        if not items:
            return True
        ok = True
        with ThreadPoolExecutor(max_workers=min(workers, len(items))) as pool:
            futures = [pool.submit(self._r2_download, url, dest) for url, dest in items]
            for fut in as_completed(futures):
                try:
                    if not fut.result():
                        ok = False
                except Exception as e:
                    logger.error(f"❌ Error en descarga paralela: {e}")
                    ok = False
        return ok

    def _r2_upload(self, local_path: Path, key: str) -> bool:
        """Subir archivo a R2.

        Se usa `put_object` directamente en vez de `upload_file`: el
        TransferManager de boto3 añade ~2,6 s de mediana por archivo a esta
        latencia (medido con 559 KB: 3362 ms vs 744 ms). Para los mockups
        (150-600 KB) una sola petición PUT es siempre mejor.
        """
        if not self.r2_enabled:
            return False
        try:
            content_type = "image/jpeg" if local_path.suffix.lower() in (".jpg", ".jpeg") else "image/png"
            with open(local_path, "rb") as fh:
                self.s3_client.put_object(
                    Bucket=self.r2_config["bucket"],
                    Key=key,
                    Body=fh.read(),
                    ContentType=content_type,
                )
            logger.info(f"✅ Mockup subido a R2: {key}")
            return True
        except Exception as e:
            logger.error(f"❌ Error subiendo a R2: {e}")
            return False

    def _upload_mockup(self, local_path: Path, key: str) -> Optional[str]:
        """Sube y devuelve la URL pública (None si falla)."""
        if self._r2_upload(local_path, key):
            return self._r2_public_url(key)
        return None

    def _r2_public_url(self, key: str) -> str:
        return f"{self.r2_config['public_url'].rstrip('/')}/{key}"

    def _r2_delete(self, key: str, private: bool = False) -> bool:
        """Borrar un objeto de R2 (best-effort, para previews efímeros)."""
        if not self.r2_enabled or not key:
            return False
        if key.lower().startswith(("http://", "https://")):
            return False
        bucket = (
            self.r2_config.get("private_bucket")
            if private
            else self.r2_config.get("bucket")
        )
        if not bucket:
            logger.warning(
                f"⚠️ No hay bucket {'privado' if private else 'público'} configurado para borrar {key}"
            )
            return False
        try:
            self.s3_client.delete_object(
                Bucket=bucket,
                Key=key,
            )
            logger.info(f"🗑️ Objeto temporal borrado de R2 ({'privado' if private else 'público'}): {key}")
            return True
        except Exception as e:
            logger.warning(f"⚠️ No se pudo borrar {key} de R2: {e}")
            return False

    def _encode_preview_image(self, local_path: Path) -> Optional[str]:
        """Devuelve la imagen como data URL para un preview efímero.

        Se reduce a 900 px de lado mayor y calidad 65 para que las 4 vistas
        quepan holgadas en el estado de Redis y en la respuesta HTTP.
        """
        try:
            img = cv2.imread(str(local_path), cv2.IMREAD_COLOR)
            if img is not None:
                h, w = img.shape[:2]
                max_dim = 900
                if max(h, w) > max_dim:
                    scale = max_dim / float(max(h, w))
                    img = cv2.resize(
                        img,
                        (max(1, int(w * scale)), max(1, int(h * scale))),
                        interpolation=cv2.INTER_AREA,
                    )
                ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 65])
                if ok:
                    b64 = base64.b64encode(buf.tobytes()).decode("ascii")
                    return f"data:image/jpeg;base64,{b64}"

            # Fallback: el archivo tal cual (solo si OpenCV no pudo leerlo).
            raw = local_path.read_bytes()
            mime = "image/jpeg" if local_path.suffix.lower() in (".jpg", ".jpeg") else "image/png"
            return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
        except Exception as e:
            logger.error(f"❌ No se pudo codificar preview {local_path}: {e}")
            return None

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
            required = []
            optional = []
            for angle, files in renders.items():
                angle_dir = self.get_render_path(product, angle)
                angle_dir.mkdir(parents=True, exist_ok=True)

                # El job trae la versión VIGENTE: se sobrescribe si cambió el
                # ETag (un re-render subido desde el dashboard se aplica).
                for kind, local_name in (("beauty", "Beauty.png"),
                                         ("id", "ID.png"),
                                         ("uvmap", "UVMap.exr")):
                    url = files.get(kind)
                    if url:
                        required.append((url, angle_dir / local_name))

                # Multizona: máscaras por zona (ID_<zone>.png) vía URLs firmadas del job.
                # Formato A: files.masks = {zone: url}. Formato B: files.mask_<zone> = url.
                masks = files.get("masks") if isinstance(files, dict) else None
                if isinstance(masks, dict):
                    for zone, url in masks.items():
                        if url:
                            optional.append((url, angle_dir / f"ID_{zone}.png"))
                elif isinstance(files, dict):
                    for field_key, url in files.items():
                        if field_key.startswith("mask_") and url:
                            zone = field_key[len("mask_"):]
                            if zone and zone not in ("beauty", "id", "uvmap"):
                                optional.append((url, angle_dir / f"ID_{zone}.png"))

            # Jobs seguidos del mismo producto: si ya se sincronizó hace poco y
            # los passes están en disco, no se revalidan 12 ETags (~0,5 s).
            if (self._sync_ttl > 0
                    and required
                    and time.time() - self._last_sync.get(product, 0.0) < self._sync_ttl
                    and all(dest.exists() for _, dest in required)):
                logger.info(f"⚡ Renders al día para {product}; se omite la revalidación con R2")
                return True

            # En paralelo: 12 descargas en serie costaban ~3,4 s por job.
            ok = self._download_many(required)
            self._download_many(optional)  # best effort (máscaras opcionales)

            if ok:
                self._last_sync[product] = time.time()
                logger.info(f"✅ Renders sincronizados (URLs firmadas del job) para {product}")
            else:
                logger.warning(f"⚠️ Algunos renders no se pudieron descargar (URLs firmadas) para {product}")

            # Masters UV por superficie (UVMap_frente/atras.exr) vía URLs
            # firmadas. Si el job no los trae, el asset no los declara: en modo
            # firmado no hay fallback público (el bucket no es de lectura pública).
            if masters:
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

        required = []
        optional = []
        for angle in angles:
            angle_dir = self.get_render_path(product, angle)
            angle_dir.mkdir(parents=True, exist_ok=True)

            # El job trae la versión vigente (ETag decide si hay que bajarla).
            required.append((self._r2_public_url(f"renders/{product}/{angle}/Beauty.png"),
                             angle_dir / "Beauty.png"))
            required.append((self._r2_public_url(f"renders/{product}/{angle}/ID.png"),
                             angle_dir / "ID.png"))
            required.append((self._r2_public_url(f"renders/{product}/{angle}/UVMap.exr"),
                             angle_dir / "UVMap.exr"))

            # Multizona: bajar máscaras por zona (ID_<zone>.png)
            for zone in print_zones:
                if not zone:
                    continue
                optional.append((self._r2_public_url(f"renders/{product}/{angle}/ID_{zone}.png"),
                                 angle_dir / f"ID_{zone}.png"))

        ok = self._download_many(required)
        self._download_many(optional)

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
            tasks = []
            for m in masters:
                key = m.get("key")
                url = m.get("url")
                if not key or not url:
                    continue
                tasks.append((url, product_folder / f"UVMap_{key}.exr"))
            # El job trae la versión VIGENTE del master (ETag decide si cambia);
            # antes se "respetaba" el local viejo y un re-render del dashboard
            # nunca se aplicaba.
            self._download_many(tasks)
            for _, dest in tasks:
                logger.info(f"✅ Master sincronizado: {dest.name}")
            return

        # Modo público (fallback): key determinista renders/<product>/UVMap_<key>.exr
        # Claves conocidas: frente/atras (polera) + las que vengan en
        # metadata.renders.masters (pack de chapitas: primera, segunda, ...).
        if not self.r2_enabled:
            return
        try:
            meta_keys = []
            cursor = self.db_conn.cursor()
            cursor.execute("SELECT metadata FROM assets_3d WHERE model_key = %s", (product,))
            row = cursor.fetchone()
            cursor.close()
            meta = row[0] if row and row[0] else {}
            rm = (meta or {}).get("renders", {}).get("masters") if isinstance(meta, dict) else None
            if isinstance(rm, dict):
                meta_keys = [k for k in rm if k]

            if meta_keys:
                keys = meta_keys
            else:
                # Sin masters declarados en la BD: solo refrescar los que YA
                # existen localmente. Antes se probaba siempre frente/atras y,
                # como el bucket no es público, cada intento daba 403 tras
                # ~1,4 s por job (Puzzle, Calendario, etc. no tienen masters).
                keys = [k for k in ("frente", "atras")
                        if (product_folder / f"UVMap_{k}.exr").exists()]
            if not keys:
                return

            tasks = [
                (self._r2_public_url(f"renders/{product}/UVMap_{key}.exr"),
                 product_folder / f"UVMap_{key}.exr")
                for key in keys
            ]
            self._download_many(tasks)
        except Exception as e:
            logger.warning(f"⚠️ Error sincronizando masters (público): {e}")

    def _resolve_product_type(self, product: str) -> Optional[str]:
        """Resuelve el product_type del asset (assets_3d.product_type) desde PostgreSQL."""
        cache_key = f"{self.db_target}:{product}"
        if cache_key in self._product_type_cache:
            return self._product_type_cache[cache_key]
        try:
            cursor = self.db_conn.cursor()
            cursor.execute("SELECT product_type FROM assets_3d WHERE model_key = %s", (product,))
            row = cursor.fetchone()
            cursor.close()
            value = str(row[0]) if row and row[0] else None
            self._product_type_cache[cache_key] = value
            return value
        except Exception as e:
            logger.warning(f"⚠️ No se pudo resolver product_type para {product}: {e}")
            return None

    def _run_mockup_generator(self, product: str, angle: str, design_path: Path, output_dir: Path, output_filename: str, print_zone: Optional[Dict] = None, zones: Optional[List[Dict]] = None, design_back_path: Optional[Path] = None, zone_designs: Optional[Dict[str, Path]] = None, output_size: Optional[int] = None, jpeg_quality: Optional[int] = None) -> bool:
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

            uvmap_path = self.get_uvmap_path(renders_path)
            if uvmap_path:
                logger.info(f"   ✓ UVMap: {uvmap_path.name}")
            else:
                logger.warning(f"⚠️ UVMap no encontrado en {renders_path}")

            # Zonas (multizona o superficies FRENTE/BACK) con su diseño propio.
            zones_for_job = None
            if zones:
                zones_for_job = []
                for z in zones:
                    zz = dict(z)
                    z_design = (zone_designs or {}).get(z.get("key"))
                    if z_design and Path(z_design).exists():
                        zz["design"] = str(Path(z_design).absolute())
                    zones_for_job.append(zz)
                logger.info(f"   ✓ Zonas ({len(zones_for_job)}): {[z['key'] for z in zones_for_job]}")
                if zone_designs:
                    logger.info(f"   ✓ {len(zone_designs)} diseños por zona: {list(zone_designs.keys())}")

            if print_zone:
                logger.info(f"   ✓ Print zone: {print_zone}")

            # IN-PROCESS: antes se lanzaba `python main.py` por ángulo y cada
            # arranque costaba ~2,3 s (intérprete + imports). Llamando a la
            # función directamente se reutilizan las cachés de passes/UVMap y
            # cada ángulo queda en ~0,2-0,3 s.
            result = _generate_mockup(
                product_id=product,
                design_path=str(design_path.absolute()),
                render_passes_dir=str(renders_path.absolute()),
                output_dir=str(output_dir.absolute()),
                output_name=output_filename,
                uvmap_path=str(uvmap_path.absolute()) if uvmap_path else None,
                print_zone=print_zone,
                product_type=self._resolve_product_type(product),
                zones=zones_for_job,
                design_back_path=(
                    str(design_back_path.absolute())
                    if design_back_path and Path(design_back_path).exists() else None
                ),
                output_size=output_size,
                jpeg_quality=jpeg_quality,
            )

            if not isinstance(result, dict) or result.get("status") != "success":
                logger.error(f"❌ Generador falló: {(result or {}).get('error')}")
                return False

            output_file = output_dir / output_filename
            if not output_file.exists():
                logger.error(f"❌ Archivo no generado: {output_file}")
                return False

            logger.info(f"✅ Mockup generado ({result.get('resolution')}): {output_file.name}")
            return True

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

        # Preview efímero: no se suben mockups a R2 ni se persiste nada.
        # Las imágenes se devuelven como data URLs en el estado de Redis (TTL
        # corto) y el diseño temporal se borra al terminar.
        preview = bool(job_data.get("preview"))
        preview_ttl = 900 if preview else None

        # BD del job: el backend la manda en `dbTarget` (local/neon). Para jobs
        # antiguos sin ese campo, se detecta buscando su fila en `mockups`.
        db_target = job_data.get("dbTarget")
        if db_target not in self.db_urls:
            detected = None if preview else self._detect_job_db(job_id)
            db_target = detected or self._default_db_target
        if not self._switch_db(db_target):
            logger.warning(f"⚠️ Job {job_id}: se sigue sin BD {db_target}; se actualizará solo Redis")

        # Keys y archivos temporales a borrar cuando el job es preview.
        preview_r2_keys: List[str] = []
        if preview:
            _zone_data = job_data.get("zoneDesigns") or job_data.get("zone_designs") or []
            _candidate_keys = [
                design_key,
                job_data.get("designBackKey"),
                *[zd.get("designKey") or zd.get("design_key") for zd in _zone_data],
            ]
            for _k in _candidate_keys:
                if _k and isinstance(_k, str) and not _k.lower().startswith(("http://", "https://")):
                    preview_r2_keys.append(_k)

        design_path = None
        design_back_path = None
        zone_designs: Dict[str, Path] = {}

        # La sincronización de passes (R2) y la descarga del diseño son
        # independientes: se lanzan en paralelo (~0,5 s menos por job).
        sync_pool = None
        sync_future = None
        # El UPDATE final de PostgreSQL no debe retrasar la respuesta al usuario
        # ni el siguiente job: se hace en este pool y el worker sigue.
        persistence_pool = ThreadPoolExecutor(max_workers=1)

        try:
            logger.info("="*60)
            logger.info(f"🎯 INICIANDO JOB: {job_id}")
            logger.info(f"   Producto: {product}")
            logger.info(f"   Design key: {design_key}")
            logger.info(f"   User ID: {user_id}")
            logger.info(f"   Preview: {preview}")
            logger.info("="*60)
            
            self._update_job_status(
                job_id, "processing", "Generando mockups...",
                persist=False, ttl=preview_ttl,
            )

            sync_pool = ThreadPoolExecutor(max_workers=1)
            sync_future = sync_pool.submit(
                self._sync_renders_from_r2,
                product,
                job_data.get("renders") or None,
                job_data.get("masters") or None,
            )

            design_key_normalized = design_key.replace('\\', '/')

            # Si el job trae un URL firmado del diseño, usarlo directamente
            design_url = job_data.get("designUrl") or job_data.get("design_url")

            # Nombre local ESTABLE por diseño (no por job): el ETag del archivo
            # permite reusarlo entre jobs, así que un mismo diseño no se vuelve
            # a bajar en cada pedido (~0,35 s menos). La key es inmutable.
            _design_src = design_key
            if _design_src.lower().startswith(("http://", "https://")):
                _design_src = urllib.parse.urlsplit(_design_src).path
            _design_hash = hashlib.sha1(_design_src.encode("utf-8")).hexdigest()[:16]
            _design_suffix = Path(_design_src).suffix or ".png"

            # Diseño servido hace poco y ya en disco: no se revalida el ETag
            # (~0,2 s por job). Un diseño reemplazado en R2 tiene la misma key
            # solo si se re-subió con el mismo nombre; la ventana es corta.
            _design_fresh = (
                self._sync_ttl > 0
                and time.time() - self._last_design.get(_design_src, 0.0) < self._sync_ttl
            )

            # Si design_key es una URL, descargarla desde R2
            if design_key_normalized.startswith(("http://", "https://")) or design_url:
                download_dir = self.python_app_path / "data" / "downloads"
                if design_url:
                    design_key_normalized = design_url
                design_path = download_dir / f"design_{_design_hash}{_design_suffix}"
                if _design_fresh and design_path.exists():
                    logger.info("⚡ Diseño reciente en disco; se omite la revalidación")
                    self._last_design[_design_src] = time.time()
                else:
                    logger.info(f"⬇️ Descargando diseño desde R2: {design_key_normalized[:120]}")
                    if not self._r2_download(design_key_normalized, design_path):
                        error_msg = f"No se pudo descargar el diseño desde R2: {design_key}"
                        logger.error(f"❌ {error_msg}")
                        self._update_job_status(job_id, "failed", error_msg, persist=not preview, ttl=preview_ttl)
                        return False
                    self._last_design[_design_src] = time.time()
            else:
                # Si hay R2 configurado, el designKey es una clave dentro del bucket
                # → descargarlo usando la URL pública
                if self.r2_enabled:
                    download_dir = self.python_app_path / "data" / "downloads"
                    design_path = download_dir / f"design_{_design_hash}{_design_suffix}"
                    design_url = f"{self.r2_config['public_url'].rstrip('/')}/{design_key_normalized}"
                    if _design_fresh and design_path.exists():
                        logger.info("⚡ Diseño reciente en disco; se omite la revalidación")
                    else:
                        logger.info(f"⬇️ Descargando diseño desde R2 (por key): {design_url}")
                        if not self._r2_download(design_url, design_path):
                            error_msg = f"No se pudo descargar el diseño desde R2: {design_key}"
                            logger.error(f"❌ {error_msg}")
                            self._update_job_status(job_id, "failed", error_msg, persist=not preview, ttl=preview_ttl)
                            return False
                    self._last_design[_design_src] = time.time()
                else:
                    design_path = self.base_storage_path / design_key_normalized

            if not design_path.exists():
                error_msg = f"Diseño no encontrado: {design_key}"
                logger.error(f"❌ {error_msg}")
                self._update_job_status(job_id, "failed", error_msg, persist=not preview, ttl=preview_ttl)
                return False

            # --- Diseño de la superficie ATRÁS (opcional) ---
            design_back_path = None
            design_back_key = job_data.get("designBackKey")
            design_back_url = job_data.get("designBackUrl") or job_data.get("design_back_url")
            if design_back_key or design_back_url:
                dl_dir = self.python_app_path / "data" / "downloads"
                dl_dir.mkdir(parents=True, exist_ok=True)
                _back_src = str(design_back_key or design_back_url or "back.png")
                if _back_src.lower().startswith(("http://", "https://")):
                    _back_src = urllib.parse.urlsplit(_back_src).path
                _back_hash = hashlib.sha1(_back_src.encode("utf-8")).hexdigest()[:16]
                suffix = Path(_back_src).suffix or ".png"
                design_back_path = dl_dir / f"design_back_{_back_hash}{suffix}"

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
                        self._update_job_status(job_id, "failed", error_msg, persist=not preview, ttl=preview_ttl)
                        return False

                logger.info(f"⬇️ Descargando diseño trasero: {str(src)[:120]}")
                if self._r2_download(str(src), design_back_path):
                    logger.info(f"✅ Diseño trasero descargado: {design_back_path}")
                else:
                    error_msg = f"No se pudo descargar el diseño trasero: {design_back_key}"
                    logger.error(f"❌ {error_msg}")
                    self._update_job_status(job_id, "failed", error_msg, persist=not preview, ttl=preview_ttl)
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
                    _z_src = str(z_design_key or z_design_url or "zone.png")
                    if _z_src.lower().startswith(("http://", "https://")):
                        _z_src = urllib.parse.urlsplit(_z_src).path
                    _z_hash = hashlib.sha1(_z_src.encode("utf-8")).hexdigest()[:16]
                    suffix = Path(_z_src).suffix or ".png"
                    z_path = dl_dir / f"design_zone_{_z_hash}{suffix}"
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
                            self._update_job_status(job_id, "failed", f"Diseño de zona '{z_key}' no encontrado", persist=not preview, ttl=preview_ttl)
                            return False
                    logger.info(f"⬇️ Descargando diseño de zona '{z_key}': {str(src)[:120]}")
                    if self._r2_download(str(src), z_path):
                        zone_designs[z_key] = z_path
                        logger.info(f"✅ Diseño de zona '{z_key}' descargado: {z_path.name}")
                    else:
                        error_msg = f"No se pudo descargar el diseño de zona '{z_key}'"
                        logger.error(f"❌ {error_msg}")
                        self._update_job_status(job_id, "failed", error_msg, persist=not preview, ttl=preview_ttl)
                        return False
                if zone_designs:
                    logger.info(f"✅ Diseños por zona listos: {list(zone_designs.keys())}")

            # Sincronizar renders desde R2 (si el job trae URLs firmadas, esas mandan).
            # Si no baja nada, se mantiene el comportamiento actual: disco local.
            # Corría en paralelo con la descarga del diseño (lanzado al inicio).
            if sync_future is not None:
                try:
                    sync_future.result()
                except Exception as e:
                    logger.warning(f"⚠️ Error sincronizando renders: {e}")

            # Detectar ángulos disponibles para este producto
            angles = self.get_angles_for_product(product)
            
            if not angles:
                error_msg = f"No se encontraron renders para el producto: {product}"
                logger.error(f"❌ {error_msg}")
                self._update_job_status(job_id, "failed", error_msg, persist=not preview, ttl=preview_ttl)
                return False
            
            logger.info(f"📐 Ángulos a generar: {len(angles)}")
            for angle in angles:
                logger.info(f"   - {angle}")
            
            # Guardar ángulos en PostgreSQL (solo si cambiaron: eran ~0,15 s por job)
            angles_cache_key = f"{self.db_target}:{product}"
            if self._angles_cache.get(angles_cache_key) != list(angles):
                try:
                    cursor = self.db_conn.cursor()
                    cursor.execute("""
                        UPDATE assets_3d 
                        SET angles = %s, updated_at = NOW()
                        WHERE model_key = %s
                    """, (angles, product))
                    self.db_conn.commit()
                    self._angles_cache[angles_cache_key] = list(angles)
                    logger.info(f"✅ Ángulos guardados en BD para {product}: {angles}")
                except Exception as e:
                    logger.warning(f"⚠️ No se pudieron guardar ángulos en BD: {e}")
            else:
                logger.debug(f"Ángulos de {product} sin cambios en BD")
            
            failed_angles = []
            # Slots por ángulo: las subidas van en paralelo (se solapan con la
            # generación del siguiente ángulo) pero el orden de mockupUrls se
            # conserva (mockupUrls[0] = vista principal = miniatura del carrito).
            mockup_slots: List[Optional[str]] = [None] * len(angles)
            upload_pool = ThreadPoolExecutor(max_workers=4)
            upload_futures: Dict = {}
            published = [0]  # último nº de URLs publicado (evita Redis writes de más)

            def _collect_uploads(wait_all: bool = False):
                """Recoge subidas terminadas y publica el progreso en orden."""
                try:
                    for fut in list(upload_futures):
                        if not wait_all and not fut.done():
                            continue
                        slot_idx, ang = upload_futures.pop(fut)
                        try:
                            url = fut.result()
                        except Exception as exc:
                            logger.error(f"❌ Subida {ang}: {exc}")
                            url = None
                        if url:
                            mockup_slots[slot_idx] = url
                        else:
                            failed_angles.append(ang)
                    done = [u for u in mockup_slots if u]
                    # Publicar UNA sola vez en cuanto está la primera imagen
                    # (la del carrito) y nada más hasta el estado final: cada
                    # setex a Upstash cuesta ~150 ms y frena la generación.
                    if published[0] == 0 and done:
                        published[0] = len(done)
                        # Solo Redis: el poll del frontend lee de ahí y así no
                        # se paga un UPDATE de PostgreSQL por cada ángulo.
                        self._update_job_status(
                            job_id, "processing",
                            f"Generados {len(done)}/{len(angles)} ángulos",
                            mockup_urls=done, persist=False,
                        )
                except Exception as e:
                    logger.error(f"❌ Error recogiendo subidas: {e}")

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
                # PACK con ID por sección: las zonas se arman POR ÁNGULO desde su
                # ID.png (ver loop). NO se derivan máscaras de los masters -> no
                # se crean los ID_<key>.png. Solo si el ID NO separa secciones se
                # cae al modelo de masters/máscara.
                id_pack = any(
                    self._id_has_sections(self.get_render_path(product, angle))
                    for angle in angles
                )
                if id_pack:
                    logger.info(f"👇 Producto {product}: zonas por ID.png por ángulo (pack)")
                else:
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

                    output_filename = f"mockup_{job_id}_{product}_{angle}.jpg"

                    # El PRIMER ángulo es la vista principal (mockupUrls[0], la
                    # que se guarda en el carrito/orden). Va a más resolución que
                    # los extras, pero no a 1024/q97: eso pesa ~500 KB y con una
                    # subida lenta (60 KB/s) son ~8 s de espera para nada (el
                    # preview se ve a ~500 px). 896/q88 pesa ~200 KB.
                    # Los demás son solo la galería: 768/q85 (~60 KB).
                    # Todo configurable por .env.
                    is_main_angle = (index == 1)
                    if is_main_angle:
                        angle_size = int(os.getenv("MOCKUP_MAIN_SIZE", "896") or 896)
                        angle_quality = int(os.getenv("MOCKUP_MAIN_QUALITY", "88") or 88)
                    else:
                        angle_size = int(os.getenv("MOCKUP_PREVIEW_SIZE", "768") or 768)
                        angle_quality = int(os.getenv("MOCKUP_PREVIEW_QUALITY", "85") or 85)

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
                    # PACK (ID.png con secciones): angle_zones queda [] -> composición
                    # de UNA sola zona con el ID.png + UVMap.exr del ángulo (sin
                    # dividir el ID ni crear ID_<key>.png).

                    if render_by_angle and angle in render_by_angle:
                        # Re-render con diseño horneado (textura real, misma que el GLB)
                        mockup_file = output_dir / output_filename
                        src = Path(render_by_angle[angle])
                        if output_filename.lower().endswith((".jpg", ".jpeg")):
                            _img = cv2.imread(str(src), cv2.IMREAD_COLOR)
                            if _img is not None:
                                cv2.imwrite(str(mockup_file), _img,
                                            [cv2.IMWRITE_JPEG_QUALITY, 90])
                            else:
                                shutil.copy2(src, mockup_file)
                        else:
                            shutil.copy2(src, mockup_file)
                        success = mockup_file.exists()
                        if success:
                            logger.info(f"✅ [{index}/{len(angles)}] Mockup (Blender): {angle}")
                    else:
                        success = self._run_mockup_generator(product, angle, design_path, output_dir, output_filename, print_zone=print_zone, zones=angle_zones, design_back_path=design_back_path, zone_designs=zone_designs, output_size=angle_size, jpeg_quality=angle_quality)
                    
                    if success:
                        mockup_file = output_dir / output_filename
                        
                        if mockup_file.exists():
                            mockup_dest_name = f"mockup_{job_id}_{product}_{angle}.jpg"
                            mockup_key = f"outputs/{mockup_dest_name}"

                            if preview:
                                # Sin R2: se devuelve la imagen en memoria. No
                                # se publican URLs parciales para no repetir el
                                # base64 en cada poll del frontend.
                                url = self._encode_preview_image(mockup_file)
                                if url:
                                    mockup_slots[index - 1] = url
                                    done = [u for u in mockup_slots if u]
                                    published[0] = len(done)
                                    self._update_job_status(
                                        job_id, "processing",
                                        f"Generados {len(done)}/{len(angles)} ángulos",
                                        persist=False, ttl=preview_ttl,
                                    )
                                else:
                                    failed_angles.append(angle)
                                try:
                                    mockup_file.unlink()
                                except Exception:
                                    pass
                            elif self.r2_enabled:
                                # En paralelo: la subida se solapa con la
                                # generación del siguiente ángulo.
                                fut = upload_pool.submit(self._upload_mockup, mockup_file, mockup_key)
                                upload_futures[fut] = (index - 1, angle)

                                # La vista principal (mockupUrls[0], la del
                                # carrito) se publica en cuanto termina SU
                                # subida: no hay que esperar a los otros 3
                                # ángulos. Antes el aviso salía ~1 s más tarde.
                                if index == 1:
                                    try:
                                        url = fut.result()
                                    except Exception as exc:
                                        logger.error(f"❌ Subida {angle}: {exc}")
                                        url = None
                                    upload_futures.pop(fut, None)
                                    if url:
                                        mockup_slots[0] = url
                                        done = [u for u in mockup_slots if u]
                                        published[0] = len(done)
                                        self._update_job_status(
                                            job_id, "processing",
                                            f"Generados {len(done)}/{len(angles)} ángulos",
                                            mockup_urls=done, persist=False,
                                        )
                                    else:
                                        failed_angles.append(angle)
                            else:
                                mockup_dest = self.base_storage_path / "outputs" / mockup_dest_name
                                mockup_dest.parent.mkdir(parents=True, exist_ok=True)
                                
                                shutil.copy2(mockup_file, mockup_dest)
                                logger.info(f"✅ [{index}/{len(angles)}] Mockup copiado: {angle}")
                                
                                mockup_slots[index - 1] = mockup_key

                            _collect_uploads()
                        else:
                            failed_angles.append(angle)
                            logger.error(f"❌ [{index}/{len(angles)}] Archivo no encontrado: {angle}")
                    else:
                        failed_angles.append(angle)
                        logger.error(f"❌ [{index}/{len(angles)}] Error generando: {angle}")
                        
                except Exception as e:
                    failed_angles.append(angle)
                    logger.error(f"❌ [{index}/{len(angles)}] Excepción en {angle}: {e}", exc_info=True)

            # Esperar las subidas que falten y armar mockupUrls en orden.
            _collect_uploads(wait_all=True)
            upload_pool.shutdown(wait=True)
            mockup_urls = [u for u in mockup_slots if u]
            
            if len(mockup_urls) > 0:
                logger.info("="*60)
                logger.info(f"✅ JOB COMPLETADO: {job_id}")
                logger.info(f"   Ángulos exitosos: {len(mockup_urls)}/{len(angles)}")
                if failed_angles:
                    logger.warning(f"   Ángulos fallidos: {', '.join(failed_angles)}")
                logger.info("="*60)
                
                mockup_url = (
                    mockup_urls[0]
                    if preview
                    else f"{self.render_url}/api/mockup/{job_id}/download"
                )

                # Redis primero (el poll del frontend lee de ahí): en cuanto se
                # escribe, el job está "completed" para el usuario. El UPDATE de
                # PostgreSQL (~0,5 s) se hace después en un hilo para no retrasar
                # ni el estado ni el siguiente job. En preview no hay fila.
                self._update_job_status(
                    job_id,
                    "completed",
                    f"Mockups generados: {len(mockup_urls)}/{len(angles)} ángulos",
                    mockup_url=mockup_url,
                    mockup_urls=mockup_urls,
                    persist=False,
                    ttl=preview_ttl,
                )
                if not preview:
                    # Conexión aislada: el job siguiente puede cambiar de BD
                    # mientras este UPDATE corre en segundo plano.
                    persistence_pool.submit(
                        self._update_postgres,
                        job_id, "completed", mockup_url, db_target, True,
                    )
                return True
            else:
                error_msg = f"Todos los ángulos fallaron: {', '.join(failed_angles)}"
                logger.error(f"❌ {error_msg}")
                self._update_job_status(job_id, "failed", error_msg, persist=not preview, ttl=preview_ttl)
                return False

        except Exception as e:
            error_msg = f"Error procesando job: {str(e)}"
            logger.error(f"❌ {error_msg}", exc_info=True)
            self._update_job_status(job_id, "failed", error_msg, persist=not preview, ttl=preview_ttl)
            return False
        finally:
            # Preview efímero: borrar diseños temporales (locales y R2) y
            # cualquier mockup que haya quedado en disco. El resultado ya está
            # en Redis como data URL, así que no se pierde nada.
            if preview:
                for _p in [design_path, design_back_path, *zone_designs.values()]:
                    if _p:
                        try:
                            Path(_p).unlink(missing_ok=True)
                        except Exception:
                            pass
                for _k in preview_r2_keys:
                    # El diseño temporal del preview vive en el bucket privado.
                    self._r2_delete(_k, private=True)
                try:
                    for _f in (self.python_app_path / "data" / "output").glob(f"mockup_{job_id}_*"):
                        _f.unlink(missing_ok=True)
                except Exception:
                    pass

            # Red de seguridad: si el job salió antes del join, se espera la
            # sincronización para que no compita con el próximo job.
            if sync_future is not None:
                try:
                    sync_future.result(timeout=30)
                except Exception:
                    pass
            if sync_pool is not None:
                sync_pool.shutdown(wait=False)
            # El pool de persistencia se cierra pero NO se espera: el UPDATE de
            # PostgreSQL termina en segundo plano mientras corre el próximo job.
            persistence_pool.shutdown(wait=False)

    def run(self):
        """Ejecutar worker en loop infinito"""
        logger.info("="*60)
        logger.info("🔄 WORKER INICIADO (MODO DINÁMICO)")
        logger.info(f"🔄 Cola Redis: mockup:jobs")
        logger.info("="*60)
        
        while True:
            try:
                result = self.redis_client.brpop("mockup:jobs", timeout=self.brpop_timeout)
                
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
                        self.redis_client = self._create_redis_client()
                        
            except KeyboardInterrupt:
                logger.info("🛑 Worker detenido por usuario (Ctrl+C)")
                break
            except redis.ConnectionError as e:
                logger.error(f"💥 Error de conexión Redis: {e}")
                logger.info("⏳ Reintentando en 5 segundos...")
                time.sleep(5)
            except redis.TimeoutError as e:
                logger.warning(f"⏱️ Timeout de Redis (reintentando): {e}")
                time.sleep(1)
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

        if not acquire_instance_lock():
            logger.error("❌ Ya hay otro worker corriendo en este equipo; este proceso se cierra.")
            logger.error("   (Dos workers se reparten los jobs y cada uno pierde la caché de passes.)")
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