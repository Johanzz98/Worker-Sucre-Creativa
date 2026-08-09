import cv2
import numpy as np
from pathlib import Path
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class SimplePassLoader:
    """Carga render passes (PNG, JPG) de Blender"""
    
    @staticmethod
    def load_pass(render_passes_dir: str, pass_name: str, required: bool = False) -> Optional[np.ndarray]:
        """
        Busca y carga un render pass por nombre
        
        Args:
            render_passes_dir: Directorio con los passes
            pass_name: Nombre del pass (ej: "Beauty", "ID", "AO")
            required: Si True, lanza excepción si no encuentra el pass
            
        Returns:
            Array numpy normalizado [0,1], o None si no se encuentra
        """
        patterns = [f"*{pass_name}*.png", f"*{pass_name}*.jpg"]
        
        for pattern in patterns:
            files = list(Path(render_passes_dir).glob(pattern))
            if files:
                file_path = files[0]
                logger.info(f"✓ {file_path.name}")
                return SimplePassLoader._load_image(file_path)
        
        if required:
            raise FileNotFoundError(f"Pass requerido no encontrado: {pass_name}")
        
        logger.warning(f"Pass opcional no encontrado: {pass_name}")
        return None
    
    @staticmethod
    def _load_image(file_path: Path) -> np.ndarray:
        """
        Carga imagen y normaliza a [0,1] float32
        
        Args:
            file_path: Ruta al archivo de imagen
            
        Returns:
            Array numpy normalizado [0,1]
        """
        img = cv2.imread(str(file_path), cv2.IMREAD_UNCHANGED)
        
        if img is None:
            raise ValueError(f"No se pudo cargar imagen: {file_path}")
        
        # Convertir BGR a RGB si es color
        if len(img.shape) == 3:
            img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        
        # Normalizar según tipo de dato
        if img.dtype == np.uint16:
            img = img.astype(np.float32) / 65535.0
        else:
            img = img.astype(np.float32) / 255.0
        
        return img