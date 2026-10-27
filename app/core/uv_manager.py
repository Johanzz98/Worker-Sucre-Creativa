from pathlib import Path
from functools import lru_cache
import logging
from typing import Optional
import numpy as np
from app.core.exr_loader import EXRLoader

logger = logging.getLogger(__name__)


@lru_cache(maxsize=64)
def _load_uv_cached(path_str: str, mtime_ns: int, size: int, normalize: bool):
    """Caché del UVMap ya decodificado (el EXR tarda ~0.1 s por ángulo).

    La clave incluye `normalize` porque el modo SUPERFICIES (regiones por
    master) necesita las coordenadas absolutas del atlas. La variante sin
    normalizar se deriva de la normalizada (ver `load_uv_raw`), así que en la
    práctica solo se decodifica el EXR una vez por archivo."""
    return EXRLoader.load_exr_file(Path(path_str), normalize=normalize)


def _load_uv_raw(path_str: str, mtime_ns: int, size: int) -> Optional[np.ndarray]:
    """UVMap en coordenadas ABSOLUTAS del atlas (normalize=False).

    Antes se decodificaba el EXR otra vez solo para tener la versión sin
    normalizar (≈0,25 s extra por ángulo). La normalización es un min/max por
    canal, así que se puede invertir exactamente sobre la versión cacheada:
        raw = normalized * (max - min) + min
    `_load_uv_cached(..., normalize=True)` guarda la referencia absoluta del
    array, que es el normalizado; aquí solo se resuelven min/max.
    """
    try:
        p = Path(path_str)
        st = p.stat()
        norm = _load_uv_cached(str(p.resolve()), st.st_mtime_ns, st.st_size, True)
        if norm is None:
            return None
        raw = _load_uv_cached(str(p.resolve()), st.st_mtime_ns, st.st_size, False)
        if raw is not None:
            return raw
        u = norm[:, :, 0]
        v = norm[:, :, 1]
        # _normalize_channel clipa a [0,1]: si el normalizado toca 0 y 1, el
        # min/max reales son exactamente 0/1 y la inversión es exacta.
        u_min, u_max = float(u.min()), float(u.max())
        v_min, v_max = float(v.min()), float(v.max())
        u_raw = u * (u_max - u_min) + u_min
        v_raw = v * (v_max - v_min) + v_min
        return np.stack([u_raw, v_raw, np.zeros_like(u_raw)], axis=-1)
    except Exception as e:
        logger.error(f"❌ Error derivando UV sin normalizar: {e}")
        return None


class UVMapManager:
    """Maneja la carga de UV maps desde archivos EXR"""
    
    @staticmethod
    def load_uv_map(render_passes_dir: str, normalize: bool = True) -> Optional[np.ndarray]:
        """
        Carga UV map desde archivo EXR en el directorio de render passes
        
        Args:
            render_passes_dir: Directorio que contiene UVMap.exr
            normalize: Normalizar canales a [0,1] (True). False conserva las
                coordenadas absolutas del atlas (para regiones por master).
            
        Returns:
            Array numpy con UV map, o None si no existe
        """
        uv_exr_path = Path(render_passes_dir) / "UVMap.exr"
        
        if not uv_exr_path.exists():
            logger.error(f"UV Map no encontrado: {uv_exr_path}")
            return None

        st = uv_exr_path.stat()
        if normalize:
            return _load_uv_cached(str(uv_exr_path.resolve()), st.st_mtime_ns,
                                   st.st_size, True)
        return _load_uv_raw(str(uv_exr_path.resolve()), st.st_mtime_ns, st.st_size)
    
    @staticmethod
    def load_uv_map_from_file(uvmap_file_path: str, normalize: bool = True) -> Optional[np.ndarray]:
        """
        Carga UV map desde una ruta de archivo específica
        (Útil para UVMaps compartidos entre múltiples orientaciones)
        
        Args:
            uvmap_file_path: Ruta completa al archivo UVMap.exr
            normalize: Normalizar canales a [0,1] (True). False conserva las
                coordenadas absolutas del atlas (para regiones por master).
            
        Returns:
            Array numpy con UV map, o None si no se puede cargar
        """
        try:
            uvmap_path = Path(uvmap_file_path)
            
            if not uvmap_path.exists():
                logger.error(f"UV Map no encontrado: {uvmap_path}")
                return None
            
            logger.info(f"📍 Cargando UV Map desde: {uvmap_path.name}")
            st = uvmap_path.stat()
            if normalize:
                uv_map = _load_uv_cached(str(uvmap_path.resolve()), st.st_mtime_ns,
                                         st.st_size, True)
            else:
                # Ruta sin normalizar: se deriva de la cacheada (sin re-decodificar).
                uv_map = _load_uv_raw(str(uvmap_path.resolve()), st.st_mtime_ns,
                                      st.st_size)
            
            if uv_map is None:
                logger.error(f"Error cargando UV Map: {uvmap_path}")
                return None
            
            logger.info(f"✅ UV Map cargado: {uv_map.shape[1]}x{uv_map.shape[0]} canales={uv_map.shape[2] if len(uv_map.shape) > 2 else 1}")
            return uv_map
            
        except Exception as e:
            logger.error(f"❌ Error cargando UV Map desde {uvmap_file_path}: {e}")
            return None