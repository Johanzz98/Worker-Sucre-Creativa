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
                     uv_map: np.ndarray, mask: np.ndarray,
                     flip_vertical: bool = True,
                     aspect_factor: float = 1.0) -> np.ndarray:
        """
        Genera mockup extrayendo iluminación del beauty pass
        
        Args:
            beauty: Beauty pass (taza blanca con iluminación completa)
            design: Diseño del usuario
            uv_map: UV map del producto
            mask: Máscara de la región de diseño
            flip_vertical: Voltear el diseño verticalmente (coordenadas UV).
                True para tazas (histórico), False para poleras/otros.
            aspect_factor: Factor de corrección de proporción de aspecto.
                Multiplica el muestreo horizontal (U) del diseño. Un valor < 1
                hace que el diseño se vea más estrecho (menos ancho), compensando
                el estiramiento horizontal generado por la relación U/V del UVMap.
                Se mide con un test de cuadrado negro: si un cuadrado de diseño se
                ve como w×h con w/h = R, el factor correcto es 1/R.
            
        Returns:
            Mockup final con diseño integrado
        """
        logger.info("🎨 Compositor de Producción - Extracción de iluminación...")
        
        # 0. Asegurar que UV map tenga las mismas dimensiones que el beauty pass
        h, w = beauty.shape[:2]
        if uv_map.shape[:2] != (h, w):
            logger.info(f"🔧 Redimensionando UV Map de {uv_map.shape[1]}x{uv_map.shape[0]} a {w}x{h}")
            uv_map = cv2.resize(uv_map, (w, h), interpolation=cv2.INTER_LINEAR)
        
        # 1. Voltear diseño verticalmente (coordinadas UV) según el producto
        design_flipped = np.flipud(design) if flip_vertical else design
        
        # 2. Aplicar diseño a través del UV map (CON FIX DE BORDES)
        design_applied = SimpleCompositor._apply_design_simple(design_flipped, uv_map, mask, aspect_factor)
        
        # 3. Extraer iluminación del beauty y aplicarla al diseño
        result = SimpleCompositor._blend_with_lighting(beauty, design_applied, mask)
        
        return result
    
    @staticmethod
    def create_mockup_multi(beauty: np.ndarray, design: np.ndarray,
                            zones: list, flip_vertical: bool = True,
                            aspect_factor: float = 1.0) -> np.ndarray:
        """
        Genera mockup componiendo el diseño en MÚLTIPLES zonas de impresión.

        A diferencia de create_mockup (una sola zona), aquí cada zona aporta su
        máscara occlusion-aware (qué píxeles muestra esa zona desde la cámara)
        y su UVMap. Las zonas son disjuntas en pantalla (el pixel más cercano a
        cámara pertenece a una sola malla), así que se suman sus contribuciones
        sobre el beauty original.

        Args:
            beauty: Beauty pass con iluminación completa
            design: Diseño del usuario (fallback o por zona si 'design' en z)
            zones: Lista de dicts {'uv_map': (H,W,3) [0,1], 'mask': (H,W) 2D [0,1],
                                   'design'?: (H,W,C) diseño propio de la zona}
            flip_vertical: Voltear el diseño verticalmente (coordenadas UV)
            aspect_factor: Corrección de proporción de aspecto

        Returns:
            Mockup final con diseño integrado en todas las zonas
        """
        logger.info(f"🎨 Compositor MULTIZONA - pasando por {len(zones)} zonas")
        h, w = beauty.shape[:2]

        smooth_masks = []
        prepared = []
        for z in zones:
            m = z['mask']
            if m.ndim == 3:
                m = np.mean(m, axis=2)
            uv = z['uv_map']
            if uv.shape[:2] != (h, w):
                uv = cv2.resize(uv, (w, h), interpolation=cv2.INTER_LINEAR)
            if m.shape[:2] != (h, w):
                m = cv2.resize(m, (w, h), interpolation=cv2.INTER_LINEAR)
            smooth_masks.append(SimpleCompositor._smooth_mask(m, blur_size=9))
            prepared.append({
                'uv_map': uv,
                'mask': m,
                'u_shift': z.get('u_shift', 0.0),
                'design': z.get('design', design),
            })

        union = np.maximum.reduce(smooth_masks)
        union_region = union > 0.5

        if np.any(union_region):
            b_in = beauty[union_region]
            white_base = np.array([
                np.percentile(b_in[:, 0], 90),
                np.percentile(b_in[:, 1], 90),
                np.percentile(b_in[:, 2], 90),
            ])
            logger.info(f"⚪ Material base MULTIZONA detectado: RGB{tuple(round(c, 3) for c in white_base)}")
        else:
            white_base = np.array([0.9, 0.9, 0.9])
            logger.warning("⚠️ Sin región UV multizona, usando blanco estándar")

        lighting = np.clip(beauty / np.maximum(white_base, 0.01)[np.newaxis, np.newaxis, :], 0.2, 1.8)

        design_lit = np.zeros_like(beauty, dtype=np.float32)
        for z, sm in zip(prepared, smooth_masks):
            z_design = z['design']
            z_design_flipped = np.flipud(z_design) if flip_vertical else z_design
            dm = SimpleCompositor._apply_design_simple(z_design_flipped, z['uv_map'], z['mask'], aspect_factor,
                                                       u_shift=z.get('u_shift', 0.0))
            design_lit += dm * lighting * sm[:, :, np.newaxis]

        result = beauty * (1.0 - union[:, :, np.newaxis]) + design_lit
        result = np.clip(result, 0.0, 1.0)

        logger.info(f"✅ Composición MULTIZONA completada")
        logger.info(f"   Output range: [{result.min():.3f}, {result.max():.3f}]")
        return result

    @staticmethod
    def create_mockup_surfaces(beauty: np.ndarray, zones: list, flip_vertical: bool = True) -> np.ndarray:
        """
        Genera mockup componiendo UN diseño por superficie usando SOLO la región
        UV de cada superficie (modelo FRENTE/BACK, sin máscaras por zona).

        Las superficies del producto se segmentan por su región UV en el atlas
        (derivada de los masters UVMap_frente.exr / UVMap_atras.exr). Cada zona
        es un diccionario:
            {
                'uv_map': (H,W,3) [0,1]   # UVMap unificado del ángulo
                'region': {'umin','umax','vmin','vmax'}  # bbox de la superficie
                'design': (H,W,C) [0,1]   # diseño de ESA superficie
                'aspect_factor': float    # (opcional) corrección de aspecto
            }
        El diseño de cada superficie se muestrea normalizado a su región, de modo
        que llena exactamente la malla de esa superficie sin deformarse (si el
        lienzo del editor tiene el mismo aspecto que la región).
        """
        logger.info(f"🎨 Compositor SUPERFICIES - {len(zones)} superficies por región UV")
        h, w = beauty.shape[:2]

        smooth_masks = []
        prepared = []
        for z in zones:
            region = z['region']
            m = SimpleCompositor._mask_from_region(z['uv_map'], region)
            if m.shape[:2] != (h, w):
                m = cv2.resize(m, (w, h), interpolation=cv2.INTER_LINEAR)
            uv = z['uv_map']
            if uv.shape[:2] != (h, w):
                uv = cv2.resize(uv, (w, h), interpolation=cv2.INTER_LINEAR)
            smooth_masks.append(SimpleCompositor._smooth_mask(m, blur_size=9))
            prepared.append({'design': z['design'], 'uv_map': uv, 'mask': m,
                             'region': z['region'], 'aspect_factor': z.get('aspect_factor', 1.0)})

        union = np.maximum.reduce(smooth_masks)
        union_region = union > 0.5

        if np.any(union_region):
            b_in = beauty[union_region]
            white_base = np.array([
                np.percentile(b_in[:, 0], 90),
                np.percentile(b_in[:, 1], 90),
                np.percentile(b_in[:, 2], 90),
            ])
            logger.info(f"⚪ Material base SUPERFICIES detectado: RGB{tuple(round(c, 3) for c in white_base)}")
        else:
            white_base = np.array([0.9, 0.9, 0.9])
            logger.warning("⚠️ Sin región UV, usando blanco estándar")

        lighting = np.clip(beauty / np.maximum(white_base, 0.01)[np.newaxis, np.newaxis, :], 0.2, 1.8)

        design_lit = np.zeros_like(beauty, dtype=np.float32)
        for z, sm in zip(prepared, smooth_masks):
            design = z['design']
            design_flipped = np.flipud(design) if flip_vertical else design
            dm = SimpleCompositor._apply_design_simple(
                design_flipped, z['uv_map'], z['mask'],
                aspect_factor=z['aspect_factor'], region=z['region'],
            )
            design_lit += dm * lighting * sm[:, :, np.newaxis]

        result = beauty * (1.0 - union[:, :, np.newaxis]) + design_lit
        result = np.clip(result, 0.0, 1.0)

        logger.info(f"✅ Composición SUPERFICIES completada")
        return result

    @staticmethod
    def _mask_from_region(uv_map: np.ndarray, region: dict) -> np.ndarray:
        """
        Máscara binaria 2D de una superficie: píxeles cuyo UV cae dentro de la
        región (bbox) de esa superficie en el atlas. Las superficies del plan A
        son disjuntas en UV (mitades del atlas), así que la clasificación por
        región no deja píxeles ambiguos.
        """
        U = uv_map[:, :, 0]
        V = uv_map[:, :, 1]
        m = (
            (U >= region['umin']) & (U <= region['umax'])
            & (V >= region['vmin']) & (V <= region['vmax'])
        )
        return m.astype(np.float32)

    @staticmethod
    def _smooth_mask(mask: np.ndarray, erode_iterations: int = 2, blur_size: int = 9) -> np.ndarray:
        """Máscara erosionada y suavizada (reutilizada por blend y multizona)"""
        g = mask if mask.ndim == 2 else np.mean(mask, axis=2)
        kernel = np.ones((3, 3), np.uint8)
        er = cv2.erode((g * 255).astype(np.uint8), kernel, iterations=erode_iterations)
        er = er.astype(np.float32) / 255.0
        return cv2.GaussianBlur(er, (blur_size, blur_size), 0)

    @staticmethod
    def crop_to_print_zone(design: np.ndarray, print_zone: dict) -> np.ndarray:
        """
        Recorta el diseño a la zona de impresión del producto.

        El print_zone viene como rect normalizado [0,1] relativo al diseño completo
        (el canvas del editor). Recorta la región central correspondiente y la
        devuelve, de modo que ese rect llena exactamente la superficie a imprimir.

        Args:
            design: Diseño cargado (H, W, C) en [0,1]
            print_zone: {x, y, width, height} normalizados [0,1] (x,y = centro o esquina
                según 'origin'; por defecto se interpreta x,y como centro del rect).

        Returns:
            Diseño recortado (tamaño nuevo). Si print_zone inválido, devuelve design sin cambios.
        """
        try:
            h, w = design.shape[:2]

            origin = print_zone.get("origin", "center")
            width = float(print_zone.get("width", 1.0))
            height = float(print_zone.get("height", 1.0))
            px = float(print_zone.get("x", 0.5))
            py = float(print_zone.get("y", 0.5))

            if width <= 0 or height <= 0 or width > 2 or height > 2:
                logger.warning(f"⚠️ print_zone inválido (w={width}, h={height}), se ignora")
                return design

            cw = max(1, int(round(w * width)))
            ch = max(1, int(round(h * height)))

            if origin == "center":
                cx = int(round(px * w))
                cy = int(round(py * h))
                x0 = max(0, min(w - cw, int(cx - cw / 2)))
                y0 = max(0, min(h - ch, int(cy - ch / 2)))
            else:  # top-left
                x0 = max(0, min(w - cw, int(round(px * w))))
                y0 = max(0, min(h - ch, int(round(py * h))))

            crop = design[y0:y0 + ch, x0:x0 + cw]
            if crop.size == 0:
                logger.warning("⚠️ print_zone produjo recorte vacío, se ignora")
                return design
            return crop
        except Exception as e:
            logger.warning(f"⚠️ Error aplicando print_zone: {e}")
            return design

    @staticmethod
    def _apply_design_simple(design: np.ndarray, uv_map: np.ndarray, mask: np.ndarray,
                            aspect_factor: float = 1.0,
                            region: dict | None = None,
                            u_shift: float = 0.0) -> np.ndarray:
        """
        Mapea el diseño usando coordenadas UV CON EROSIÓN DE MÁSCARA
        
        Args:
            design: Diseño volteado verticalmente
            uv_map: Mapa UV normalizado [0,1]
            mask: Máscara para erosionar bordes
            aspect_factor: Corrección de proporción de aspecto (multiplica U).
                < 1 estrecha el diseño horizontalmente.
            region: Bbox {'umin','umax','vmin','vmax'} de la superficie en el
                atlas. Si se define, el diseño se muestrea NORMALIZADO a esa
                región (el diseño completo llena la malla de la superficie).
            u_shift: Desplaza el muestreo U para alinear el centro del diseño
                (0.5) con el centro de la isla UV (gorra/chapa). Solo aplica en
                modo máscara (sin región).

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
        
        # Normalización a la región de la superficie si se define (modelo FRENTE/BACK):
        # el diseño llena la bbox de la región, no el atlas completo.
        if region:
            umin, umax = region['umin'], region['umax']
            vmin, vmax = region['vmin'], region['vmax']
            U = np.clip((U - umin) / max(umax - umin, 1e-6), 0.0, 1.0)
            V = np.clip((V - vmin) / max(vmax - vmin, 1e-6), 0.0, 1.0)
            logger.info(f"📏 UV Mapping por región: U[{umin:.3f},{umax:.3f}] V[{vmin:.3f},{vmax:.3f}]")
        
        logger.info(f"📏 UV Mapping:")
        logger.info(f"   Design: {design_w}×{design_h}")
        logger.info(f"   UV Map: {w}×{h}")
        logger.info(f"   UV range: U[{U.min():.3f}, {U.max():.3f}] V[{V.min():.3f}, {V.max():.3f}]")
        logger.info(f"   Aspect factor: {aspect_factor}")
        
        # Convertir UV [0,1] a coordenadas de pixel
        # aspect_factor comprime el muestreo horizontal para compensar el
        # estiramiento por la relación U/V del UVMap (medido con test de cuadrado).
        # u_shift desplaza el muestreo U para centrar el diseño en la isla
        # (gorra/chapa): map_x = (U + u_shift) * width.
        u_off = u_shift if not region else 0.0
        map_x = (U + u_off) * (design_w - 1) * aspect_factor
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