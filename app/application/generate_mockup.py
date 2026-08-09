import cv2
import numpy as np
from pathlib import Path
import logging
from typing import Dict, Optional

from app.core.exr_loader import EXRLoader
from app.core.pass_loader import SimplePassLoader
from app.core.uv_manager import UVMapManager
from app.core.compositor import SimpleCompositor

logger = logging.getLogger(__name__)


def generate_mockup(
    product_id: str, 
    design_path: str, 
    render_passes_dir: str, 
    output_dir: str,
    output_name: Optional[str] = None,
    uvmap_path: Optional[str] = None  # ← NUEVO PARÁMETRO
) -> Dict[str, str]:
    """
    Genera mockup usando compositor simple con flip vertical
    
    Args:
        product_id: ID del producto (ej: "mug")
        design_path: Ruta al diseño del usuario
        render_passes_dir: Directorio con los render passes (Beauty.png, ID.png)
        output_dir: Directorio donde guardar el resultado
        output_name: Nombre personalizado para el archivo (opcional)
        uvmap_path: Ruta específica al UVMap.exr (opcional, para UVMaps compartidos)
        
    Returns:
        Dict con status, product, output_file, resolution o error
    """
    try:
        logger.info(f"🚀 GENERACIÓN SIMPLE: {product_id}")
        
        # 1. Cargar diseño
        design = SimplePassLoader._load_image(Path(design_path))
        logger.info(f"📐 Diseño original: {design.shape[1]}x{design.shape[0]}")
        
        # 2. Cargar UV Map (con soporte para ruta personalizada)
        if uvmap_path:
            logger.info(f"🗺️  Usando UVMap compartido: {uvmap_path}")
            uv_map = UVMapManager.load_uv_map_from_file(uvmap_path)
        else:
            logger.info(f"🗺️  Buscando UVMap en: {render_passes_dir}")
            uv_map = UVMapManager.load_uv_map(render_passes_dir)
        
        if uv_map is None:
            error_msg = f"No se encontró UV Map en: {uvmap_path or render_passes_dir}"
            logger.error(f"❌ {error_msg}")
            return {
                "status": "error",
                "product": product_id,
                "error": error_msg
            }
        
        logger.info(f"✅ UV Map cargado: {uv_map.shape[1]}x{uv_map.shape[0]}")
        
        # 3. Cargar beauty pass
        beauty = SimplePassLoader.load_pass(render_passes_dir, "Beauty", required=True)
        h, w = beauty.shape[:2]
        logger.info(f"📐 Render: {w}x{h}")
        
        # 4. Cargar o generar máscara
        mask = SimplePassLoader.load_pass(render_passes_dir, "ID")
        if mask is None:
            mask = np.any(beauty > 0.05, axis=2).astype(np.float32)
            logger.info("🎭 Máscara generada automáticamente desde beauty")
        else:
            if len(mask.shape) == 3:
                mask = np.mean(mask, axis=2)
            mask = (mask > 0.1).astype(np.float32)
        
        logger.info(f"🎭 Máscara: {np.sum(mask > 0)} píxeles")
        
        # 5. Composición simple
        final_result = SimpleCompositor.create_mockup(beauty, design, uv_map, mask)
        
        # 6. Guardar resultado con nombre personalizado o por defecto
        if output_name:
            output_path = Path(output_dir) / output_name
            logger.info(f"📝 Usando nombre personalizado: {output_name}")
        else:
            output_path = Path(output_dir) / f"mockup_{product_id}.png"
            logger.info(f"📝 Usando nombre por defecto: mockup_{product_id}.png")
        
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        final_uint8 = (final_result * 255).astype(np.uint8)
        final_bgr = cv2.cvtColor(final_uint8, cv2.COLOR_RGB2BGR)
        cv2.imwrite(str(output_path), final_bgr)
        
        logger.info(f"✅ ÉXITO: {output_path}")
        
        return {
            "status": "success",
            "product": product_id,
            "output_file": str(output_path),
            "resolution": f"{w}x{h}"
        }
        
    except Exception as e:
        logger.error(f"❌ Error generando mockup: {e}", exc_info=True)
        return {
            "status": "error",
            "product": product_id,
            "error": str(e)
        }