"""
Re-render con Blender del mesh de impresión de la gorra/chapita.

Equivalente al modo FRENTE/BACK (render_frontback.py) pero para un único mesh
de impresión: aplica el diseño del editor como textura sobre la isla UV real
(Cylinder.001) y re-renderiza con CYCLES de la escena. Resultado pixel-igual al
preview GLB (mismas UVs, mismo canvas, offset idéntico a ModelArea) y con
sombreado real.

Configuración (fallback a compositor si falta algo):
  CHAPITA_BLEND            -> ruta del .blend de producción (gorra).
                              Default: C:\\Users\\johan\\Documents\\Chapita\\Chapita.blend
  BLENDER_BIN              -> ruta del ejecutable de Blender.
                              Default: detecta Blender Foundation en Program Files
  CHAPITA_RENDER_SIZE      -> resolución cuadrada (default: la del .blend, 2048)
  CHAPITA_RENDER_SAMPLES   -> muestras Cycles (default: las del .blend, 4096)
  CHAPITA_RENDER_TIMEOUT   -> segundos máx del render (por defecto 900)
"""
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).resolve().parents[2] / "blender" / "scripts"
RENDER_SCRIPT = SCRIPT_DIR / "render_mesh_designs.py"

DEFAULT_BLEND = Path(r"C:\Users\johan\Documents\Chapita\Chapita.blend")


def find_blender() -> Optional[str]:
    env_bin = os.getenv("BLENDER_BIN")
    if env_bin and Path(env_bin).exists():
        return env_bin
    if shutil.which(env_bin or "blender"):
        return env_bin or "blender"
    base = Path(os.getenv("PROGRAMFILES", r"C:\Program Files")) / "Blender Foundation"
    if base.is_dir():
        versions = sorted([p for p in base.iterdir() if p.is_dir()], key=lambda p: p.name)
        for v in reversed(versions):
            exe = v / "blender.exe"
            if exe.exists():
                return str(exe)
    return None


def find_blend() -> Optional[Path]:
    env_blend = os.getenv("CHAPITA_BLEND")
    if env_blend and Path(env_blend).exists():
        return Path(env_blend)
    default = Path(__file__).resolve().parents[1] / "data" / "blends" / "chapita.blend"
    if default.exists():
        return default
    if DEFAULT_BLEND.exists():
        return DEFAULT_BLEND
    return None


def render_mesh_designs(
    design_path: Path,
    output_dir: Path,
    mesh: str = "Cylinder.001",
    output_name: str = "blender.png",
    timeout: Optional[int] = None,
) -> Optional[Dict[str, Path]]:
    """
    Renderiza la escena con el diseño horneado sobre el mesh de impresión.
    Devuelve {angle: png} o None si no se puede usar Blender (config/fallback).
    El blend actual solo tiene un ángulo (la cámara de la escena), así que el
    puesto 'centro' es el render directo.
    """
    blender_bin = find_blender()
    blend = find_blend()
    if blender_bin is None:
        logger.warning("⚠️ No se encontró Blender (BLENDER_BIN o Blender Foundation) -> compositor")
        return None
    if blend is None:
        logger.warning("⚠️ No se encontró el .blend de la gorra (CHAPITA_BLEND) -> compositor")
        return None
    if not RENDER_SCRIPT.exists():
        logger.error(f"❌ script Blender no existe: {RENDER_SCRIPT} -> compositor")
        return None

    timeout = timeout or int(os.getenv("CHAPITA_RENDER_TIMEOUT", "900"))
    work_dir = output_dir / "blender_render"
    work_dir.mkdir(parents=True, exist_ok=True)

    cmd = [blender_bin, "--background", str(blend), "--python", str(RENDER_SCRIPT), "--",
           "--design", str(design_path), "--out", str(work_dir),
           "--mesh", mesh, "--name", output_name]
    size = os.getenv("CHAPITA_RENDER_SIZE")
    samples = os.getenv("CHAPITA_RENDER_SAMPLES")
    if size:
        cmd += ["--size", size]
    if samples:
        cmd += ["--samples", samples]

    logger.info(f"🎬 Render mesh por partes (Blender): {' '.join(str(c) for c in cmd)}")
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logger.error(f"❌ Blender excedió {timeout}s -> compositor")
        return None
    except Exception as e:
        logger.error(f"❌ error ejecutando Blender: {e} -> compositor")
        return None

    (work_dir / "blender.log").write_text((result.stdout or "") + "\n--- STDERR ---\n" + (result.stderr or ""), encoding="utf-8", errors="replace")

    log = (result.stdout or "")[-4000:]
    if result.returncode != 0:
        logger.error(f"❌ Blender falló ({result.returncode}):\n{log}\n{(result.stderr or '')[-2000:]}")
        return None

    png = work_dir / output_name
    if not (png.exists() and png.stat().st_size > 0):
        logger.error(f"❌ Blender no escribió {png}:\n{log}")
        return None

    logger.info(f"🎬 Blender OK: {png} ({png.stat().st_size} bytes)")
    return {"centro": png}