import numpy as np
from pathlib import Path
import logging
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import OpenEXR
    import Imath
    EXR_AVAILABLE = True
except ImportError:
    EXR_AVAILABLE = False
    logger.warning("OpenEXR no disponible")


class EXRLoader:
    """Carga archivos EXR para UV maps"""
    
    @staticmethod
    def load_exr_file(exr_path: Path) -> Optional[np.ndarray]:
        """
        Carga archivo EXR y retorna UV map normalizado
        
        Args:
            exr_path: Ruta al archivo EXR
            
        Returns:
            Array numpy (H, W, 3) con UV map normalizado, o None si falla
        """
        if not EXR_AVAILABLE:
            logger.error("OpenEXR no está instalado")
            return None
        
        try:
            exr_file = OpenEXR.InputFile(str(exr_path))
            header = exr_file.header()
            dw = header['dataWindow']
            
            width = dw.max.x - dw.min.x + 1
            height = dw.max.y - dw.min.y + 1
            
            X = EXRLoader._read_channel(exr_file, 'X', height, width)
            Y = EXRLoader._read_channel(exr_file, 'Y', height, width)
            
            exr_file.close()
            
            if X is not None and Y is not None:
                U_normalized = EXRLoader._normalize_channel(X, "U (X)")
                V_normalized = EXRLoader._normalize_channel(Y, "V (Y)")
                
                uv_map = np.stack([U_normalized, V_normalized, np.zeros_like(U_normalized)], axis=-1)
                
                logger.info(f"✅ EXR cargado - U: [{U_normalized.min():.3f}, {U_normalized.max():.3f}], "
                           f"V: [{V_normalized.min():.3f}, {V_normalized.max():.3f}]")
                
                return uv_map
            
        except Exception as e:
            logger.error(f"Error cargando EXR: {e}")
        
        return None
    
    @staticmethod
    def _read_channel(exr_file, channel: str, height: int, width: int) -> Optional[np.ndarray]:
        """Lee un canal del archivo EXR"""
        try:
            channel_str = exr_file.channel(channel, Imath.PixelType(Imath.PixelType.FLOAT))
            return np.frombuffer(channel_str, dtype=np.float32).reshape(height, width)
        except Exception as e:
            logger.error(f"Error leyendo canal {channel}: {e}")
            return None
    
    @staticmethod
    def _normalize_channel(channel: np.ndarray, name: str) -> np.ndarray:
        """Normaliza canal a rango [0, 1]"""
        channel_min = channel.min()
        channel_max = channel.max()
        
        if channel_max - channel_min < 0.001:
            logger.warning(f"Canal {name} tiene rango muy pequeño")
            return channel
        
        channel_normalized = (channel - channel_min) / (channel_max - channel_min)
        return np.clip(channel_normalized, 0.0, 1.0)