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
from psycopg2.extras import RealDictCursor
from pathlib import Path
from typing import Dict, List
from dotenv import load_dotenv

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
        Busca el archivo UVMap en la carpeta de renders
        Soporta diferentes nombres: UVMap.exr, UVMap_centro.exr, etc.
        """
        candidates = ["UVMap.exr", "UVMap_centro.exr", "UVMap_derecha.exr", "UVMap_izquierda.exr"]
        
        for candidate in candidates:
            test_path = renders_path / candidate
            if test_path.exists():
                return test_path
        
        return None

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

    def _run_mockup_generator(self, product: str, angle: str, design_path: Path, output_dir: Path, output_filename: str) -> bool:
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
                "--design", str(design_path.absolute()),
                "--render_passes", str(renders_path.absolute()),
                "--output", str(output_dir.absolute()),
                "--output-name", output_filename
            ]
            
            # Buscar UVMap específico para este ángulo
            uvmap_path = self.get_uvmap_path(renders_path)
            if uvmap_path:
                cmd.extend(["--uvmap", str(uvmap_path.absolute())])
                logger.info(f"   ✓ UVMap: {uvmap_path.name}")
            else:
                logger.warning(f"⚠️ UVMap no encontrado en {renders_path}")
            
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

            # Si design_key es una URL, descargarla desde R2
            if design_key_normalized.startswith(("http://", "https://")):
                download_dir = self.python_app_path / "data" / "downloads"
                suffix = Path(design_key_normalized).suffix or ".png"
                design_path = download_dir / f"design_{job_id}{suffix}"
                logger.info(f"⬇️ Descargando diseño desde R2: {design_key_normalized}")
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

            output_dir = self.python_app_path / "data" / "output"
            
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
            
            for index, angle in enumerate(angles, 1):
                try:
                    logger.info(f"🔄 [{index}/{len(angles)}] Generando ángulo: {angle}")
                    
                    output_filename = f"mockup_{job_id}_{product}_{angle}.png"
                    success = self._run_mockup_generator(product, angle, design_path, output_dir, output_filename)
                    
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