import cv2
import numpy as np
from pathlib import Path
import logging
from typing import Dict, List, Optional

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
    uvmap_path: Optional[str] = None,  # ← NUEVO PARÁMETRO
    print_zone: Optional[Dict] = None,   # ← NUEVO PARÁMETRO: zona de impresión del producto
    product_type: Optional[str] = None,   # ← Tipo de producto (assets_3d.product_type)
    zones: Optional[List[Dict]] = None,    # ← NUEVO PARÁMETRO: zonas de impresión (frente/atras)
    design_back_path: Optional[str] = None  # ← NUEVO PARÁMETRO: diseño superficie ATRÁS
) -> Dict[str, str]:
    """
    Genera mockup usando compositor simple con flip vertical
    
    Args:
        product_id: ID del producto (ej: "mug")
        design_path: Ruta al diseño del usuario (superficie FRENTE)
        render_passes_dir: Directorio con los render passes (Beauty.png, ID.png)
        output_dir: Directorio donde guardar el resultado
        output_name: Nombre personalizado para el archivo (opcional)
        uvmap_path: Ruta específica al UVMap.exr (opcional, para UVMaps compartidos)
        print_zone: Zona de impresión del producto (opcional). Dict normalizado [0,1]
            relativo al diseño: {x, y, width, height}. Si se define, el diseño se
            recorta a esa zona centrada antes de mapearlo a la superficie UV.
        zones: Zonas de impresión (opcional). Modelo heredado multizona: cada zona
            es {key, uvmap (ruta abs), mask (ruta abs)}. Modelo FRENTE/BACK: cada
            zona es {key, uvmap (ruta abs, opcional -> usa el unificado), region:
            {umin, umax, vmin, vmax}}. En modo FRENTE/BACK la máscara se deriva
            automáticamente de la región UV (sin ID_<zone>.png) y cada zona usa
            su diseño (design_back_path para la zona 'atras').
        design_back_path: Ruta al diseño de la superficie ATRÁS (opcional).
    
    Returns:
        Dict con status, product, output_file, resolution o error
    """
    try:
        logger.info(f"🚀 GENERACIÓN SIMPLE: {product_id}")
        
        # 1. Cargar diseño
        design = SimplePassLoader._load_image(Path(design_path))
        logger.info(f"📐 Diseño FRENTE: {design.shape[1]}x{design.shape[0]}")

        design_back = None
        if design_back_path:
            design_back = SimplePassLoader._load_image(Path(design_back_path))
            logger.info(f"📐 Diseño ATRÁS: {design_back.shape[1]}x{design_back.shape[0]}")

        # NOTA: ya NO se recorta al print_zone. El mock usa el canvas completo
        # (igual que el GLB del editor) para que no se deforme el diseño.
        
        # 2. Cargar UV Map (con soporte para ruta personalizada)
        # En modo SUPERFICIES las regiones de frente/atrás se comparan en
        # coordenadas ABSOLUTAS del atlas, así que el UVMap del ángulo también
        # debe cargarse sin normalizar (la normalización min/max se conserva
        # para el path heredado de una sola zona / multizona).
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
        
        # 5. Composición
        # El flip vertical se necesita cuando el UVMap viene con el eje V en
        # orientación de imagen (es el caso de tazas/mug y de los UVMaps
        # regenerados desde la pieza de impresión en poleras: Polerablancadefault,
        # poleraNormali, y cualquier variante de color/camisa cuyo tipo sea polera).
        # Se decide por el product_type del asset (el modelKey puede ser arbitrario:
        # ej. "xxxxxxxxxxxxxxxxxxxxx"), con fallback al propio modelKey.
        product_lower = (product_id or '').lower()
        product_type_lower = (product_type or '')
        flip_vertical = (
            'taza' in product_lower
            or 'mug' in product_lower
            or 'polera' in product_lower
            or 'taza' in product_type_lower
            or 'mug' in product_type_lower
            or 'polera' in product_type_lower
            or 'tshirt' in product_type_lower
            # Chapita / Chapa / Cylinder.001: máscara ID.png + flip vertical.
            # Es la única corrección para la gorra (sale volteada de abajo
            # hacia arriba).
            or 'chapita' in product_lower
            or 'chapa' in product_lower
            or 'cylinder.001' in product_lower
        )
        logger.info(f"🔄 Flip vertical del diseño: {flip_vertical} (producto: {product_id}, tipo: {product_type or 'no informado'})")

        # Corrección de proporción de aspecto: compensa el estiramiento/desplazamiento
        # horizontal del diseño en el panel. Calibrado por el usuario (test de cuadrados)
        # para poleraNormali: el canvas vertical correcto (W/H~0.76) ya imprime el
        # cuadrado como cuadrado, por lo que el factor queda en 1.0 (sin compensación).
        aspect_factor = 1.0
        logger.info(f"📐 Aspect factor: {aspect_factor} (producto: {product_id})")

        if zones:
            # Modelo FRENTE/BACK: cada zona trae región UV (sin máscaras) y un diseño propio.
            # Con UN solo diseño (sin design_back), la superficie 'frente' se imprime y la
            # 'atras' queda en blanco (igual que el GLB del editor con una sola cara).
            surface_mode = any('region' in (z or {}) for z in zones)
            if surface_mode:
                    logger.info(f"🧩 Composición SUPERFICIES (FRENTE/BACK) con {len(zones)} zonas por región UV")
                    # Cada superficie se pinta por su REGIÓN UV (máscara por oclusión
                    # real, ver _mask_from_region), no por el nombre de la carpeta del
                    # ángulo. Antes se apagaba 'atras' cuando el ángulo se llamaba
                    # 'centro', lo que borraba la espalda en cualquier ángulo que no
                    # se llamara exactamente 'derecha'/'izquierda' (p.ej. todos los
                    # 'centro' inclinados). La región UV ya decide qué se ve.
                    angle_name = Path(render_passes_dir or "").name.lower()
                    logger.info(f"📐 Ángulo '{angle_name}': se componen todas las superficies por región UV")

                    zone_configs = []
                    for z in zones:
                        key = z.get("key", "?")
                        region = z.get("region")
                        if not region:
                            raise ValueError(f"Zona '{key}': falta 'region' en modo superficies")

                        # Coordenadas ABSOLUTAS del atlas (normalize=False): las
                        # regiones de los masters se comparan con el UVMap del ángulo
                        # en el mismo espacio.
                        z_uv_path = z.get("uvmap") or uvmap_path
                        if z_uv_path and not Path(z_uv_path).is_absolute():
                            z_uv_path = str(Path(render_passes_dir) / z_uv_path)
                        if z_uv_path:
                            z_uv = UVMapManager.load_uv_map_from_file(z_uv_path, normalize=False)
                        else:
                            z_uv = UVMapManager.load_uv_map(render_passes_dir, normalize=False)
                        if z_uv is None:
                            raise ValueError(f"Zona '{key}': no se pudo cargar UVMap")

                        # El diseño de cada superficie: 'atras' usa design_back, el resto el frente.
                        # Sin design_back (un solo diseño), la espalda no se imprime.
                        if key == 'atras' and design_back is None:
                            logger.info(f"🚫 Zona 'atras' sin diseño trasero -> se omite (queda blanca)")
                            continue
                        z_design = design_back if key == 'atras' else design
                        z_aspect = float(z.get("aspect_factor", 1.0))

                        zone_configs.append({
                            "uv_map": z_uv,
                            "region": region,
                            "design": z_design,
                            "aspect_factor": z_aspect,
                            "key": key,
                        })
                        logger.info(f"🗺️  Zona '{key}' | región U[{region['umin']:.3f},{region['umax']:.3f}] V[{region['vmin']:.3f},{region['vmax']:.3f}]")
                        logger.info(f"   Diseño: {z_design.shape[1]}×{z_design.shape[0]} | Aspect factor: {z_aspect}")

                    final_result = SimpleCompositor.create_mockup_surfaces(
                        beauty, zone_configs, flip_vertical=flip_vertical,
                    )
                # El UVMap unificado se reutiliza/cierra en memoria; nada que liberar aquí.
            else:
                logger.info(f"🧩 Composición MULTIZONA con {len(zones)} zonas")
                zone_configs = []
                for z in zones:
                    key = z.get("key")
                    z_uv_path = z.get("uvmap") or uvmap_path
                    z_id_path = z.get("mask")

                    if z_uv_path and not Path(z_uv_path).is_absolute():
                        z_uv_path = str(Path(render_passes_dir) / z_uv_path)
                    if (not z_uv_path) and key:
                        candidate = Path(render_passes_dir) / f"UVMap_{key}.exr"
                        if candidate.exists():
                            z_uv_path = str(candidate)
                    if not z_uv_path:
                        raise ValueError(f"Zona '{key}': falta uvmap y no existe UVMap_{key}.exr")

                    z_uv = UVMapManager.load_uv_map_from_file(z_uv_path)
                    if z_uv is None:
                        raise ValueError(f"Zona '{key}': no se pudo cargar UVMap en {z_uv_path}")

                    if z_id_path and not Path(z_id_path).is_absolute():
                        z_id_path = str(Path(render_passes_dir) / z_id_path)
                    if (not z_id_path) and key:
                        candidate = Path(render_passes_dir) / f"ID_{key}.png"
                        if candidate.exists():
                            z_id_path = str(candidate)
                    if not z_id_path:
                        raise ValueError(f"Zona '{key}': falta mask y no existe ID_{key}.png")

                    z_mask_img = SimplePassLoader._load_image(Path(z_id_path))
                    if len(z_mask_img.shape) == 3:
                        z_mask_img = np.mean(z_mask_img, axis=2)
                    z_mask = (z_mask_img > 0.1).astype(np.float32)

                    # Diseño propio de la zona (pack: cada chapita su diseño). Si
                    # no viene, la zona usa el diseño principal (modelo heredado).
                    z_design = design
                    z_design_path = z.get("design")
                    if z_design_path:
                        if not Path(z_design_path).is_absolute():
                            z_design_path = str(Path(render_passes_dir) / z_design_path)
                        z_design = SimplePassLoader._load_image(Path(z_design_path))
                        logger.info(f"🎨 Zona '{key}': diseño propio {z_design.shape[1]}x{z_design.shape[0]}")

                    # Polera per-mesh: la superficie 'atras' usa el diseño trasero
                    # (design_back). Sin design_back, la espalda queda blanca.
                    if not z_design_path and key == 'atras' and design_back is not None:
                        z_design = design_back
                        logger.info("🎨 Zona 'atras': usando diseño trasero (design_back)")

                    z_aspect = float(z.get("aspect_factor", aspect_factor))
                    logger.info(f"🗺️  Zona '{key}' | UVMap: {z_uv_path}")
                    logger.info(f"   Máscara: {z_id_path} | píxeles: {np.sum(z_mask > 0)}")
                    logger.info(f"   Aspect factor: {z_aspect}")

                    # Gorra (chapa/chapita/Cylinder.001): centrar el diseño igual
                    # que el preview GLB. Se alinea el centro del canvas (0.5)
                    # con el centro de la isla UV visible, con centro robusto por
                    # percentiles (1-99) para no dejarse contaminar por el
                    # antialiasing del contorno del ID. Sin esto, el render cae
                    # más a la izquierda que el GLB.
                    u_shift = 0.0
                    if ('chapita' in product_lower or 'chapa' in product_lower
                            or 'cylinder.001' in product_lower):
                        um = z_uv[:, :, 0][z_mask > 0.5]
                        if um.size:
                            p1, p99 = np.percentile(um, [1.0, 99.0])
                            u_shift = 0.5 - (p1 + p99) / 2.0
                            logger.info(
                                f"↔️ U-shift de centrado (gorra): {u_shift:+.4f} "
                                f"(isla U robusta [{p1:.4f},{p99:.4f}])"
                            )

                    zone_configs.append({
                        "uv_map": z_uv,
                        "mask": z_mask,
                        "design": z_design,
                        "aspect_factor": z_aspect,
                        "u_shift": u_shift,
                    })

                final_result = SimpleCompositor.create_mockup_multi(
                    beauty, design, zone_configs,
                    flip_vertical=flip_vertical,
                )
        else:
            # Una sola superficie (taza/gorra): el diseño se mapea con la UV
            # ABSOLUTA del render (igual que el editor 3D), NO normalizada.
            # Normalizar al rango visible estira el diseño cuando la superficie
            # no se ve entera (p.ej. la taza envuelve: UV visible 0..0.745).
            uv_abs = (
                UVMapManager.load_uv_map_from_file(uvmap_path, normalize=False)
                if uvmap_path
                else UVMapManager.load_uv_map(render_passes_dir, normalize=False)
            )
            final_result = SimpleCompositor.create_mockup(
                beauty, design, uv_abs if uv_abs is not None else uv_map, mask,
                flip_vertical=flip_vertical,
                aspect_factor=aspect_factor
            )
        
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