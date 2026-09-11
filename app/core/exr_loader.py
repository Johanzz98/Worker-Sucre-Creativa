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
    """Carga archivos EXR para UV maps (soporta múltiples partes)"""
    
    @staticmethod
    def load_exr_file(exr_path: Path, normalize: bool = True) -> Optional[np.ndarray]:
        if not EXR_AVAILABLE:
            logger.error("OpenEXR no está instalado")
            return None
        
        try:
            # ============================================
            # ABRIR ARCHIVO Y VERIFICAR PARTES
            # ============================================
            exr_file = OpenEXR.InputFile(str(exr_path))
            
            # Verificar si hay múltiples partes
            parts = exr_file.parts() if hasattr(exr_file, 'parts') else [None]
            logger.info(f"📖 Partes en EXR: {len(parts)}")
            
            u_channel = None
            v_channel = None
            header = None
            width = None
            height = None
            
            # ============================================
            # BUSCAR EN CADA PARTE
            # ============================================
            for i, part in enumerate(parts):
                if part is not None:
                    # Si hay múltiples partes, usar la parte específica
                    part_header = exr_file.header(part)
                else:
                    part_header = exr_file.header()
                
                all_channels = list(part_header['channels'].keys())
                logger.info(f"📖 Parte {i}: canales = {all_channels}")
                
                # Buscar R y G (capa RGB)
                if 'R' in all_channels and 'G' in all_channels:
                    u_channel = 'R'
                    v_channel = 'G'
                    header = part_header
                    logger.info(f"✅ Encontrados R y G en parte {i}")
                    break
                # Buscar X e Y (capa default)
                elif 'X' in all_channels and 'Y' in all_channels:
                    u_channel = 'X'
                    v_channel = 'Y'
                    header = part_header
                    logger.info(f"✅ Encontrados X e Y en parte {i}")
                    break
            
            if u_channel is None or v_channel is None:
                logger.error("No se encontraron canales UV en ninguna parte")
                return None
            
            # Obtener dimensiones
            dw = header['dataWindow']
            width = dw.max.x - dw.min.x + 1
            height = dw.max.y - dw.min.y + 1
            
            # ============================================
            # LEER CANALES DE LA PARTE CORRECTA
            # ============================================
            U_data = None
            V_data = None
            
            if len(parts) > 1 and part is not None:
                # Leer de la parte específica
                try:
                    U_data = exr_file.channel(u_channel, Imath.PixelType(Imath.PixelType.FLOAT), part)
                    V_data = exr_file.channel(v_channel, Imath.PixelType(Imath.PixelType.FLOAT), part)
                    logger.info(f"✅ Leídos de parte {part}")
                except Exception as e:
                    logger.error(f"Error leyendo de parte {part}: {e}")
                    return None
            else:
                # Leer de la única parte
                try:
                    U_data = exr_file.channel(u_channel, Imath.PixelType(Imath.PixelType.FLOAT))
                    V_data = exr_file.channel(v_channel, Imath.PixelType(Imath.PixelType.FLOAT))
                    logger.info("✅ Leídos de parte única")
                except Exception as e:
                    logger.error(f"Error leyendo canales: {e}")
                    return None
            
            exr_file.close()
            
            if U_data is not None and V_data is not None:
                # Convertir a numpy
                U = np.frombuffer(U_data, dtype=np.float32).reshape(height, width)
                V = np.frombuffer(V_data, dtype=np.float32).reshape(height, width)
                
                # Normalizar a [0, 1] por canal (comportamiento original).
                # normalize=False: conserva las coordenadas ABSOLUTAS del atlas
                # (necesario para derivar regiones por master; la normalización
                # min/max por master destruiría la escala de las mitades).
                if normalize:
                    U_normalized = EXRLoader._normalize_channel(U, f"U ({u_channel})")
                    V_normalized = EXRLoader._normalize_channel(V, f"V ({v_channel})")
                else:
                    U_normalized = U
                    V_normalized = V
                
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
            logger.warning(f"Canal {name} tiene rango muy pequeño (min={channel_min:.4f}, max={channel_max:.4f})")
            return channel
        
        channel_normalized = (channel - channel_min) / (channel_max - channel_min)
        return np.clip(channel_normalized, 0.0, 1.0)