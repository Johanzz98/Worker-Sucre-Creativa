"""
Render por partes FRENTE/BACK: hornea los diseños como textura (atlas plan A)
y re-renderiza los 4 ángulos con Blender headless para que el mockup se vea
idéntico al preview GLB del editor (mismo atlas, misma malla, con sombreado).

Flujo:
  1. El worker arma el atlas 1024×1024 (frente -> mitad izquierda, atras ->
     mitad derecha, fondo blanco, flip vertical igual que el editor/GLB).
  2. Un script Blender (render_frontback_designs.py) aplica el atlas al base
     color de Polera_Frente y Polera_Atras (sus UV son las islas del atlas),
     deja Polera_Restante en blanco y renderiza los ángulos centro/derecha/
     izquierda/atras con el mismo rig del export (camara orbitante, CYCLES).
  3. El worker usa esos PNG directamente como mockups.

Configuración (fallback a compositor si falta algo):
  MOCKUP_RENDER_MODE=blender
  BLENDER_BIN              -> ruta del ejecutable de Blender ('blender' por defecto)
  POLERA_FRONTBACK_BLEND   -> ruta del .blend de producción (poleraNormaliMultiple)
                               Si no se setea, busca data/blends/poleraNormaliMultiple.blend
  POLERA_RENDER_SIZE       -> resolución cuadrada (por defecto 2048)
  POLERA_RENDER_TIMEOUT    -> segundos máx del render (por defecto 600)
"""

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

logger = logging.getLogger(__name__)

# Mismas constantes que PLAN_A_REGIONS en worker_main.py y que PLAN_A_ATLAS en
# el frontend (ModelArea.tsx). El diseño se inscribe en la isla de cada
# superficie dentro del atlas cuadrado.
ATLAS_PLAN_A = {
    "width": 1024,
    "height": 1024,
    "frente": {"umin": 0.020, "umax": 0.480, "vmin": 0.259, "vmax": 0.741},
    "atras": {"umin": 0.520, "umax": 0.980, "vmin": 0.270, "vmax": 0.730},
}

ANGLES = ["centro", "derecha", "izquierda", "atras"]

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "blender" / "scripts"
RENDER_SCRIPT = SCRIPT_DIR / "render_frontback_designs.py"
DEFAULT_BLEND = Path(__file__).resolve().parents[1] / "data" / "blends" / "poleraNormaliMultiple.blend"


def _load_rgba(path: Path) -> Optional[np.ndarray]:
    """Carga una imagen como RGBA float32 [0,1] (solo opencv/numpy)."""
    try:
        import cv2
    except ImportError:
        logger.error("se requiere opencv-python (cv2) para el atlas")
        return None
    img = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if img is None:
        return None
    img = cv2.cvtColor(img, cv2.COLOR_BGRA2RGBA) if img.shape[2] == 4 else cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    return (img.astype(np.float32)) / 255.0


def build_frontback_atlas(design_path: Path, design_back_path: Path, out_path: Path) -> bool:
    """
    Compone el atlas 1024×1024 igual que el frontend ModelArea.buildAtlas:
    cada diseño se estira dentro de su isla (vista vertical), fondo blanco,
    los píxeles transparentes se mezclan con blanco. Devuelve True si se
    escribió out_path.
    """
    try:
        import cv2
    except ImportError:
        logger.error("se requiere opencv-python (cv2) para el atlas")
        return False

    w = ATLAS_PLAN_A["width"]
    h = ATLAS_PLAN_A["height"]
    atlas = np.full((h, w, 3), 1.0, dtype=np.float32)

    designs = {
        "frente": design_path,
        "atras": design_back_path,
    }
    for key, rect in (("frente", ATLAS_PLAN_A["frente"]), ("atras", ATLAS_PLAN_A["atras"])):
        p = designs[key]
        rgba = _load_rgba(p)
        if rgba is None:
            logger.error(f"no se pudo cargar diseño para la isla '{key}': {p}")
            return False

        rw = max(1, int(round((rect["umax"] - rect["umin"]) * w)))
        rh = max(1, int(round((rect["vmax"] - rect["vmin"]) * h)))
        x0 = int(round(rect["umin"] * w))
        y0 = int(round(rect["vmin"] * h))

        resized = cv2.resize(rgba, (rw, rh), interpolation=cv2.INTER_LINEAR)
        # flip vertical (misma orientación que el worker/editor para poleras)
        resized = np.ascontiguousarray(np.flipud(resized))

        rgb = resized[:, :, :3]
        alpha = resized[:, :, 3:4]
        # transparente -> blanco del tejido
        out_rgb = np.clip(rgb * alpha + 1.0 * (1.0 - alpha), 0, 1)
        atlas[y0 : y0 + rh, x0 : x0 + rw] = out_rgb.astype(np.float32)
        logger.info(
            f"🗺️  isla '{key}' | rect U[{rect['umin']:.3f},{rect['umax']:.3f}] "
            f"V[{rect['vmin']:.3f},{rect['vmax']:.3f}] @{x0},{y0} {rw}x{rh}"
        )

    atlas_u8 = np.clip(atlas * 255.0, 0, 255).astype(np.uint8)
    cv2.imwrite(str(out_path), cv2.cvtColor(atlas_u8, cv2.COLOR_RGB2BGR))
    logger.info(f"🧩 Atlas escrito: {out_path} ({w}x{h})")
    return True


def find_blender() -> Optional[str]:
    """Resuelve el binario de Blender: env BLENDER_BIN o 'blender' en PATH."""
    env_bin = os.getenv("BLENDER_BIN")
    if env_bin and Path(env_bin).exists():
        return env_bin
    if shutil.which(env_bin or "blender"):
        return env_bin or "blender"
    return None


def find_blend() -> Optional[Path]:
    """Resuelve el .blend de producción: env POLERA_FRONTBACK_BLEND o bundled."""
    env_blend = os.getenv("POLERA_FRONTBACK_BLEND")
    if env_blend and Path(env_blend).exists():
        return Path(env_blend)
    if DEFAULT_BLEND.exists():
        return DEFAULT_BLEND
    return None


def render_frontback_designs(
    design_path: Path,
    design_back_path: Path,
    output_dir: Path,
    size: Optional[int] = None,
    timeout: Optional[int] = None,
) -> Optional[Dict[str, Path]]:
    """
    Renderiza los 4 ángulos con los diseños horneados. Devuelve {angle: png}
    o None si no se puede usar Blender (configuración/fallback compositor).
    """
    blender_bin = find_blender()
    blend = find_blend()
    if blender_bin is None:
        logger.warning("⚠️ MOCKUP_RENDER_MODE=blender sin BLENDER_BIN válido -> compositor")
        return None
    if blend is None:
        logger.warning(
            "⚠️ MOCKUP_RENDER_MODE=blender sin .blend (set POLERA_FRONTBACK_BLEND) -> compositor"
        )
        return None
    if not RENDER_SCRIPT.exists():
        logger.error(f"❌ script Blender no existe: {RENDER_SCRIPT}")
        return None

    size = size or int(os.getenv("POLERA_RENDER_SIZE", "2048"))
    timeout = timeout or int(os.getenv("POLERA_RENDER_TIMEOUT", "600"))
    work_dir = output_dir / "blender_render"
    work_dir.mkdir(parents=True, exist_ok=True)

    atlas_path = work_dir / "atlas.png"
    if not build_frontback_atlas(design_path, design_back_path, atlas_path):
        logger.error("❌ no se pudo construir el atlas; usando compositor")
        return None

    cmd = [
        blender_bin,
        "--background",
        str(blend),
        "--python",
        str(RENDER_SCRIPT),
        "--",
        "--atlas",
        str(atlas_path),
        "--out",
        str(work_dir),
        "--size",
        str(size),
    ]
    logger.info(f"🎬 Render por partes (Blender): {' '.join(str(c) for c in cmd)}")
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logger.error(f"❌ Blender excedió {timeout}s; usando compositor")
        return None
    except Exception as e:
        logger.error(f"❌ error ejecutando Blender: {e}")
        return None

    log = (result.stdout or "")[-4000:]
    if result.returncode != 0:
        logger.error(f"❌ Blender falló ({result.returncode}):\n{log}\n{result.stderr[-2000:]}")
        return None

    produced: Dict[str, Path] = {}
    for angle in ANGLES:
        png = work_dir / f"{angle}.png"
        if png.exists() and png.stat().st_size > 0:
            produced[angle] = png
            logger.info(f"  ✅ {angle}: {png} ({png.stat().st_size} bytes)")
        else:
            logger.error(f"  ❌ faltó render del ángulo '{angle}'")

    if len(produced) != len(ANGLES):
        logger.error(f"❌ render incompleto ({len(produced)}/{len(ANGLES)}); log:\n{log}")
        return None
    return produced