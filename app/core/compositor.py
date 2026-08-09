import cv2
import numpy as np
import logging

logger = logging.getLogger(__name__)


class SimpleCompositor:
    """
    Compositor optimizado para producción
    Extrae iluminación del beauty pass sin necesitar lighting passes separados
    """
    
    @staticmethod
    def create_mockup(beauty: np.ndarray, design: np.ndarray, 
                     uv_map: np.ndarray, mask: np.ndarray) -> np.ndarray:
        """
        Genera mockup extrayendo iluminación del beauty pass
        
        Args:
            beauty: Beauty pass (taza blanca con iluminación completa)
            design: Diseño del usuario
            uv_map: UV map del producto
            mask: Máscara de la región de diseño
            
        Returns:
            Mockup final con diseño integrado
        """
        logger.info("🎨 Compositor de Producción - Extracción de iluminación...")
        
        # 0. Asegurar que UV map tenga las mismas dimensiones que el beauty pass
        h, w = beauty.shape[:2]
        if uv_map.shape[:2] != (h, w):
            logger.info(f"🔧 Redimensionando UV Map de {uv_map.shape[1]}x{uv_map.shape[0]} a {w}x{h}")
            uv_map = cv2.resize(uv_map, (w, h), interpolation=cv2.INTER_LINEAR)
        
        # 1. Voltear diseño verticalmente (coordinadas UV)
        design_flipped = np.flipud(design)
        
        # 2. Aplicar diseño a través del UV map (CON FIX DE BORDES)
        design_applied = SimpleCompositor._apply_design_simple(design_flipped, uv_map, mask)
        
        # 3. Extraer iluminación del beauty y aplicarla al diseño
        result = SimpleCompositor._blend_with_lighting(beauty, design_applied, mask)
        
        return result
    
    @staticmethod
    def _apply_design_simple(design: np.ndarray, uv_map: np.ndarray, mask: np.ndarray) -> np.ndarray:
        """
        Mapea el diseño usando coordenadas UV CON EROSIÓN DE MÁSCARA
        
        Args:
            design: Diseño volteado verticalmente
            uv_map: Mapa UV normalizado [0,1]
            mask: Máscara para erosionar bordes
            
        Returns:
            Diseño mapeado al render
        """
        # Verificar que las dimensiones coincidan
        h, w = mask.shape[:2]
        if uv_map.shape[:2] != (h, w):
            logger.warning(f"⚠️  UV Map dimensions mismatch, resizing from {uv_map.shape[1]}x{uv_map.shape[0]} to {w}x{h}")
            uv_map = cv2.resize(uv_map, (w, h), interpolation=cv2.INTER_LINEAR)
        
        U = uv_map[:, :, 0]
        V = uv_map[:, :, 1]
        design_h, design_w = design.shape[:2]
        
        logger.info(f"📏 UV Mapping:")
        logger.info(f"   Design: {design_w}×{design_h}")
        logger.info(f"   UV Map: {w}×{h}")
        logger.info(f"   UV range: U[{U.min():.3f}, {U.max():.3f}] V[{V.min():.3f}, {V.max():.3f}]")
        
        # Convertir UV [0,1] a coordenadas de pixel
        map_x = U * (design_w - 1)
        map_y = V * (design_h - 1)
        
        # ============================================
        # 🔧 FIX 1: USAR BORDER_REPLICATE EN LUGAR DE CONSTANT
        # Esto replica los píxeles del borde en lugar de usar blanco
        # ============================================
        design_u8 = (design * 255).astype(np.uint8)
        remapped = cv2.remap(
            design_u8,
            map_x.astype(np.float32),
            map_y.astype(np.float32),
            interpolation=cv2.INTER_LINEAR,  # 🔧 Cambiar a LINEAR (menos artefactos)
            borderMode=cv2.BORDER_REPLICATE  # 🔧 Replicar bordes en lugar de blanco constante
        )
        
        remapped_float = remapped.astype(np.float32) / 255.0
        
        # ============================================
        # 🔧 FIX 2: EROSIONAR MÁSCARA PARA ELIMINAR BORDES PROBLEMÁTICOS
        # ============================================
        mask_gray = mask if len(mask.shape) == 2 else np.mean(mask, axis=2)
        
        # Erosionar la máscara 2-3 píxeles para eliminar bordes con artefactos
        kernel_erode = np.ones((3, 3), np.uint8)
        mask_eroded = cv2.erode((mask_gray * 255).astype(np.uint8), kernel_erode, iterations=2)
        mask_eroded = mask_eroded.astype(np.float32) / 255.0
        
        # Suavizar la máscara erosionada
        kernel_blur = 5
        mask_smooth = cv2.GaussianBlur(mask_eroded, (kernel_blur, kernel_blur), 0)
        
        # ============================================
        # 🔧 FIX 3: APLICAR MÁSCARA EROSIONADA AL DISEÑO
        # ============================================
        mask_3d = np.stack([mask_smooth] * 3, axis=-1)
        
        # Donde mask_smooth = 0 (bordes), usar blanco puro
        white_background = np.ones_like(remapped_float)
        design_masked = remapped_float * mask_3d + white_background * (1.0 - mask_3d)
        
        logger.info(f"✅ Diseño mapeado con erosión de máscara (kernel: {kernel_erode.shape[0]}×{kernel_erode.shape[1]})")
        
        return design_masked
    
    @staticmethod
    def _blend_with_lighting(beauty: np.ndarray, design: np.ndarray, 
                            mask: np.ndarray) -> np.ndarray:
        """
        Compone diseño con iluminación extraída del beauty
        
        ALGORITMO:
        1. El beauty contiene: Material_Blanco × Iluminación_Completa
        2. Detectamos el color base blanco del material
        3. Extraemos: Iluminación = Beauty / Material_Blanco
        4. Aplicamos: Diseño × Iluminación
        5. Resultado: Diseño con iluminación realista del render
        
        Args:
            beauty: Beauty pass con iluminación completa
            design: Diseño ya mapeado con UV
            mask: Máscara de la región de diseño
            
        Returns:
            Composición final
        """
        # ═══════════════════════════════════════════════════════════════
        # 1. PREPARAR MÁSCARA (CON EROSIÓN ADICIONAL)
        # ═══════════════════════════════════════════════════════════════
        if mask.ndim == 2:
            mask_3d = np.stack([mask] * 3, axis=-1)
        else:
            mask_3d = mask
        
        # Erosionar máscara para blend
        mask_gray = mask_3d[:, :, 0]
        kernel_erode = np.ones((3, 3), np.uint8)
        mask_eroded = cv2.erode((mask_gray * 255).astype(np.uint8), kernel_erode, iterations=2)
        mask_eroded = mask_eroded.astype(np.float32) / 255.0
        
        # Suavizar bordes para transiciones naturales
        kernel_size = 9  # Aumentar blur para transición más suave
        mask_smooth = cv2.GaussianBlur(mask_eroded, (kernel_size, kernel_size), 0)
        mask_3d_smooth = np.stack([mask_smooth] * 3, axis=-1)
        
        logger.info(f"🎭 Máscara erosionada y suavizada - kernel blur: {kernel_size}×{kernel_size}")
        
        # ═══════════════════════════════════════════════════════════════
        # 2. DETECTAR COLOR BASE BLANCO (AUTOMÁTICO)
        # ═══════════════════════════════════════════════════════════════
        mask_region = mask_smooth > 0.5
        
        if np.any(mask_region):
            # Detectar blanco base usando percentil 90 del área UV
            beauty_in_uv = beauty[mask_region]
            white_base_r = np.percentile(beauty_in_uv[:, 0], 90)
            white_base_g = np.percentile(beauty_in_uv[:, 1], 90)
            white_base_b = np.percentile(beauty_in_uv[:, 2], 90)
            white_base = np.array([white_base_r, white_base_g, white_base_b])
            
            logger.info(f"⚪ Material base detectado automáticamente:")
            logger.info(f"   RGB({white_base[0]:.3f}, {white_base[1]:.3f}, {white_base[2]:.3f})")
        else:
            # Fallback: blanco estándar
            white_base = np.array([0.9, 0.9, 0.9])
            logger.warning(f"⚠️ No se detectó región UV, usando blanco estándar")
        
        # ═══════════════════════════════════════════════════════════════
        # 3. EXTRAER FACTOR DE ILUMINACIÓN
        # ═══════════════════════════════════════════════════════════════
        white_base_safe = np.maximum(white_base, 0.01)
        lighting_factor = beauty / white_base_safe[np.newaxis, np.newaxis, :]
        
        # Limitar rango
        lighting_factor = np.clip(lighting_factor, 0.2, 1.8)
        
        logger.info(f"💡 Factor de iluminación extraído:")
        logger.info(f"   Min: {lighting_factor.min():.3f} (sombras)")
        logger.info(f"   Max: {lighting_factor.max():.3f} (highlights)")
        logger.info(f"   Mean: {lighting_factor.mean():.3f} (promedio)")
        
        # ═══════════════════════════════════════════════════════════════
        # 4. APLICAR ILUMINACIÓN AL DISEÑO
        # ═══════════════════════════════════════════════════════════════
        design_lit = design * lighting_factor
        
        # ═══════════════════════════════════════════════════════════════
        # 5. COMPOSICIÓN FINAL CON BLEND SUAVE
        # ═══════════════════════════════════════════════════════════════
        result = beauty * (1.0 - mask_3d_smooth) + design_lit * mask_3d_smooth
        
        # Asegurar rango válido [0, 1]
        result = np.clip(result, 0.0, 1.0)
        
        logger.info(f"✅ Composición completada")
        logger.info(f"   Output range: [{result.min():.3f}, {result.max():.3f}]")
        
        return result