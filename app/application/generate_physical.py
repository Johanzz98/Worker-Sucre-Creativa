import cv2
import numpy as np
from pathlib import Path
import logging
from typing import Dict, Any

from app.core.compositor import PhysicalCompositor
from app.core.tonemapping import apply_filmic_and_srgb

logger = logging.getLogger(__name__)


def generate_physical_mockup(
    product_id: str,
    design_path: str,
    passes_dir: str,
    output_dir: str,
    # Parámetros ajustables
    ao_strength: float = 0.5,
    glossy_contribution: float = 0.15,
    exposure: float = 0.0,
    brightness: float = 1.0,
    contrast: float = 1.0,
    saturation: float = 1.0,
) -> Dict[str, Any]:
    """
    Genera mockup con compositor físico y parámetros ajustables
    
    Args:
        product_id: ID del producto
        design_path: Ruta al diseño
        passes_dir: Directorio con render passes
        output_dir: Directorio de salida
        ao_strength: Intensidad del AO (0.0-1.0)
        glossy_contribution: Contribución de glossy (0.0-1.0)
        exposure: Exposure en stops (-2.0 a +2.0)
        brightness: Multiplicador de brillo (0.5-2.0)
        contrast: Contraste (0.5-2.0)
        saturation: Saturación (0.0-2.0)
        
    Returns:
        Dict con status y resultado
    """
    try:
        logger.info(f"🚀 GENERACIÓN FÍSICA: {product_id}")
        logger.info(f"📊 Parámetros: AO={ao_strength}, Glossy={glossy_contribution}, "
                   f"Exp={exposure:+.1f}, Bright={brightness:.2f}")
        
        compositor = PhysicalCompositor()
        
        # Helper para cargar passes
        def load_pass(name, force_rgb=False):
            files = sorted(Path(passes_dir).glob(f"*{name}*.png"))
            if not files:
                # Intentar con EXR
                files = sorted(Path(passes_dir).glob(f"*{name}*.exr"))
            if not files:
                raise FileNotFoundError(f"Pass no encontrado: {name}")
            
            img = cv2.imread(str(files[0]), cv2.IMREAD_UNCHANGED)
            if img is None:
                raise ValueError(f"No se pudo cargar: {files[0]}")
            
            if len(img.shape) == 3 and img.shape[2] == 4:
                img = img[:, :, :3]
            if len(img.shape) == 3:
                img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            
            img = compositor.normalize_image(img)
            
            if force_rgb and len(img.shape) == 2:
                img = np.stack([img]*3, axis=-1)
            
            return img
        
        # Cargar todos los passes necesarios
        logger.info("📥 Cargando passes...")
        diffuse_color = load_pass("DiffCol", force_rgb=True)
        diffuse_direct = load_pass("DiffDir", force_rgb=True)
        diffuse_indirect = load_pass("DiffInd", force_rgb=True)
        glossy_direct = load_pass("GlossDir", force_rgb=True)
        glossy_indirect = load_pass("GlossInd", force_rgb=True)
        ao_pass = load_pass("AO", force_rgb=True)
        
        # Cargar máscara UV
        try:
            id_mask = load_pass("IndexOB", force_rgb=False)
        except FileNotFoundError:
            try:
                id_mask = load_pass("ID", force_rgb=False)
            except FileNotFoundError:
                id_mask = load_pass("Mug_Porcelain", force_rgb=False)
        
        # Resize máscara si es necesario
        target_h, target_w = diffuse_color.shape[:2]
        if id_mask.shape[:2] != (target_h, target_w):
            id_mask_u8 = (np.clip(id_mask, 0, 1) * 255).astype(np.uint8)
            id_mask = cv2.resize(id_mask_u8, (target_w, target_h), 
                                interpolation=cv2.INTER_LINEAR)
            id_mask = compositor.normalize_image(id_mask)
        
        # Cargar diseño
        design = cv2.imread(design_path, cv2.IMREAD_UNCHANGED)
        if design is None:
            raise ValueError(f"No se pudo cargar diseño: {design_path}")
        
        # Manejar alpha channel
        if len(design.shape) == 3 and design.shape[2] == 4:
            rgb = design[:, :, :3].astype(np.float32)
            alpha = design[:, :, 3:4].astype(np.float32) / 255.0
            white_bg = np.ones_like(rgb) * 255.0
            design = (rgb * alpha + white_bg * (1 - alpha)).astype(np.uint8)
        
        if len(design.shape) == 3:
            design = cv2.cvtColor(design, cv2.COLOR_BGR2RGB)
        design = compositor.normalize_image(design)
        
        logger.info("✅ Passes cargados")
        
        # 1. Aplicar diseño
        logger.info("🎨 Aplicando diseño...")
        h, w = diffuse_color.shape[:2]
        mask = id_mask.copy()
        if len(mask.shape) == 3:
            mask = np.mean(mask, axis=2)
        mask_3ch = np.stack([mask]*3, axis=-1)
        
        design_resized = cv2.resize((design*255).astype(np.uint8), (w, h),
                                   interpolation=cv2.INTER_LANCZOS4)
        design_resized = compositor.normalize_image(design_resized)
        
        albedo = diffuse_color * (1.0 - mask_3ch) + design_resized * mask_3ch
        
        # 2. Light pass con glossy ajustable
        logger.info(f"💡 Construyendo light pass (glossy: {glossy_contribution})...")
        diffuse_total = diffuse_direct + diffuse_indirect
        glossy_total = glossy_direct + glossy_indirect
        light_pass = diffuse_total + (glossy_total * glossy_contribution)
        
        # 3. AO con strength ajustable
        logger.info(f"🌫️  Aplicando AO (strength: {ao_strength})...")
        ao_adjusted = 1.0 - (1.0 - ao_pass) * ao_strength
        
        # 4. Composición física
        logger.info("🔄 Composición física...")
        composite = albedo * light_pass * ao_adjusted
        
        # 5. Ajustes pre-tonemapping
        if brightness != 1.0:
            logger.info(f"☀️  Brightness: {brightness:.2f}x")
            composite = composite * brightness
        
        if contrast != 1.0:
            logger.info(f"🎚️  Contrast: {contrast:.2f}")
            composite = ((composite - 0.5) * contrast) + 0.5
            composite = np.clip(composite, 0.0, None)
        
        # 6. Tonemapping
        logger.info(f"🎛️  Tonemapping (exposure: {exposure:+.1f})...")
        final = apply_filmic_and_srgb(composite, exposure=exposure)
        
        # 7. Saturación post-tonemapping
        if saturation != 1.0:
            logger.info(f"🌈 Saturación: {saturation:.2f}")
            final_u8 = (final * 255).astype(np.uint8)
            hsv = cv2.cvtColor(final_u8, cv2.COLOR_RGB2HSV).astype(np.float32)
            hsv[:, :, 1] = np.clip(hsv[:, :, 1] * saturation, 0, 255)
            final_u8 = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2RGB)
            final = final_u8.astype(np.float32) / 255.0
        
        # 8. Guardar
        output_path = Path(output_dir) / f"mockup_{product_id}_physical.png"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        final_u8 = compositor.to_uint8(final)
        final_bgr = cv2.cvtColor(final_u8, cv2.COLOR_RGB2BGR)
        cv2.imwrite(str(output_path), final_bgr)
        
        logger.info(f"✅ Mockup guardado: {output_path}")
        logger.info(f"📊 Stats: Mean={np.mean(final):.3f}, Max={np.max(final):.3f}")
        
        return {
            "status": "success",
            "product": product_id,
            "output_file": str(output_path),
            "resolution": f"{w}x{h}"
        }
        
    except Exception as e:
        logger.error(f"❌ Error: {e}", exc_info=True)
        return {
            "status": "error",
            "product": product_id,
            "error": str(e)
        }
