import numpy as np

# =======================
#  FILMIC TONEMAPPING
# =======================

def _filmic_curve(x):
    a = 0.22
    b = 0.30
    c = 0.10
    d = 0.20
    e = 0.01
    f = 0.30
    x = np.maximum(x, 0.0)
    return ((x*(a*x + c*b) + d*e) / (x*(a*x + b) + d*f)) - (e/f)


def apply_filmic_and_srgb(linear_img: np.ndarray) -> np.ndarray:
    """
    Aplica la transformación EXACTA que Blender usa:
    1. Filmic tonemap
    2. White normalization
    3. Conversión a sRGB (gamma 2.4)
    """

    # Paso 1: Filmic
    filmic = _filmic_curve(linear_img)

    # White normalization → pista: 16.0 = punto blanco filmic
    white_point = _filmic_curve(np.array([16.0]))[0]
    filmic = filmic / white_point

    # Paso 2: Clamp
    filmic = np.clip(filmic, 0.0, 1.0)

    # Paso 3: sRGB gamma EXACTO
    srgb = np.where(
        filmic <= 0.0031308,
        filmic * 12.92,
        1.055 * np.power(filmic, 1/2.4) - 0.055,
    )

    return np.clip(srgb, 0.0, 1.0)
