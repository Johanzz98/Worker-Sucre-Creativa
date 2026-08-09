from pathlib import Path
import logging
from typing import Optional
import numpy as np
from app.core.exr_loader import EXRLoader

logger = logging.getLogger(__name__)


class UVMapManager:
    """Maneja la carga de UV maps desde archivos EXR"""
    
    @staticmethod
    def load_uv_map(render_passes_dir: str) -> Optional[np.ndarray]:
        """
        Carga UV map desde archivo EXR en el directorio de render passes
        
        Args:
            render_passes_dir: Directorio que contiene UVMap.exr
            
        Returns:
            Array numpy con UV map, o None si no existe
        """
        uv_exr_path = Path(render_passes_dir) / "UVMap.exr"
        
        if not uv_exr_path.exists():
            logger.error(f"UV Map no encontrado: {uv_exr_path}")
            return None
        
        return EXRLoader.load_exr_file(uv_exr_path)
    
    @staticmethod
    def load_uv_map_from_file(uvmap_file_path: str) -> Optional[np.ndarray]:
        """
        Carga UV map desde una ruta de archivo específica
        (Útil para UVMaps compartidos entre múltiples orientaciones)
        
        Args:
            uvmap_file_path: Ruta completa al archivo UVMap.exr
            
        Returns:
            Array numpy con UV map, o None si no se puede cargar
        """
        try:
            uvmap_path = Path(uvmap_file_path)
            
            if not uvmap_path.exists():
                logger.error(f"UV Map no encontrado: {uvmap_path}")
                return None
            
            logger.info(f"📍 Cargando UV Map desde: {uvmap_path.name}")
            uv_map = EXRLoader.load_exr_file(uvmap_path)
            
            if uv_map is None:
                logger.error(f"Error cargando UV Map: {uvmap_path}")
                return None
            
            logger.info(f"✅ UV Map cargado: {uv_map.shape[1]}x{uv_map.shape[0]} canales={uv_map.shape[2] if len(uv_map.shape) > 2 else 1}")
            return uv_map
            
        except Exception as e:
            logger.error(f"❌ Error cargando UV Map desde {uvmap_file_path}: {e}")
            return None