import numpy as np


def _filmic_curve(x):
    """Curva filmic EXACTA de Blender"""
    a = 0.22
    b = 0.30
    c = 0.10
    d = 0.20
    e = 0.01
    f = 0.30
    x = np.maximum(x, 0.0)
    return ((x*(a*x + c*b) + d*e) / (x*(a*x + b) + d*f)) - (e/f)


def apply_filmic_and_srgb(linear_img: np.ndarray, exposure: float = 0.0) -> np.ndarray:
    """
    Pipeline EXACTO de Blender:
    1. Exposure (en stops, no multiplicativo)
    2. Filmic tonemap
    3. White normalization con punto 11.2
    4. Conversión a sRGB
    
    Args:
        linear_img: Imagen en espacio lineal
        exposure: Exposure en stops (0.0 = sin cambios, 1.0 = 2x brillante)
        
    Returns:
        Imagen en sRGB [0,1]
    """
    
    # Paso 1: Aplicar exposure como 2^exposure
    if exposure != 0.0:
        linear_img = linear_img * np.power(2.0, exposure)
    
    # Paso 2: Filmic curve
    filmic = _filmic_curve(linear_img)
    
    # Paso 3: White normalization - USAR 11.2 como Blender
    white_point = _filmic_curve(np.array([11.2]))[0]
    filmic = filmic / white_point
    
    # Paso 4: Clamp antes de sRGB
    filmic = np.clip(filmic, 0.0, 1.0)
    
    # Paso 5: Conversión a sRGB (gamma 2.4)
    srgb = np.where(
        filmic <= 0.0031308,
        filmic * 12.92,
        1.055 * np.power(filmic, 1/2.4) - 0.055
    )
    
    return np.clip(srgb, 0.0, 1.0)


def srgb_to_linear(srgb_img: np.ndarray) -> np.ndarray:
    """
    Convierte de sRGB a lineal
    
    Args:
        srgb_img: Imagen en espacio sRGB
        
    Returns:
        Imagen en espacio lineal
    """
    linear = np.where(
        srgb_img <= 0.04045,
        srgb_img / 12.92,
        np.power((srgb_img + 0.055) / 1.055, 2.4)
    )
    return linear