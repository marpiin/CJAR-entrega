"""
Validación de todo lo que entra al sistema:

- parse_input_image: foto como ruta, bytes o array -> imagen BGR de 3 canales.
- parse_input_template: array, lista o texto -> vector float32 de 512 valores con norma 1.
  Rechaza NaN, infinitos, vectores de ceros y dimensiones distintas de 512.
- parse_input_id: identificador apto para guardarlo en el CSV.
"""

import io
import os
import unicodedata
from pathlib import Path
from typing import Optional, Union, List, Tuple

# Límites contra "bombas" de imagen: un PNG de pocos KB puede declarar 50000x50000 px
# y agotar la memoria al decodificarlo. El límite de OpenCV se fija en src/__init__.py, pero solo
# vale si `src` se importa antes que cv2 (web/app.py importa cv2 primero: allí lo cubre el ENV del
# Dockerfile). Ver el docstring de src/__init__.py.
from src import MAX_IMAGE_PIXELS
MAX_IMAGE_BYTES = 30 * 1024 * 1024

import cv2
import numpy as np
from PIL import Image

Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS

EXPECTED_EMBEDDING_DIM = 512
# Un template en texto son 512 números: más de esto es basura o un intento de agotar memoria
MAX_TEMPLATE_CHARS = 64 * EXPECTED_EMBEDDING_DIM
MAX_ID_LENGTH = 128


def parse_input_id(user_id) -> str:
    """
    Valida el identificador de una persona antes de usarlo en la BD.

    Raises:
        ValueError: Si es vacío, demasiado largo, tiene caracteres de control (saltos de
            línea, nulos...), contiene '/' o '\\' o empieza por '=', '+', '-' o '@' (inyección de fórmulas si
            alguien abre el CSV con Excel).
        TypeError: Si no es texto ni número entero.
    """
    if isinstance(user_id, bool) or not isinstance(user_id, (str, int, np.integer)):
        raise TypeError(f"Tipo de dato no soportado para 'id': {type(user_id)}")
    clean = str(user_id).strip()
    if not clean:
        raise ValueError("El 'id' no puede ser una cadena vacía.")
    if len(clean) > MAX_ID_LENGTH:
        raise ValueError(f"El 'id' supera los {MAX_ID_LENGTH} caracteres.")
    if any(unicodedata.category(c)[0] == "C" for c in clean):
        raise ValueError("El 'id' contiene caracteres de control.")
    if clean[0] in "=+-@":
        raise ValueError("El 'id' no puede empezar por '=', '+', '-' ni '@'.")
    if "/" in clean or "\\" in clean or clean in (".", ".."):
        # La API direcciona a las personas por /api/personas/{id}: un id con barra no se
        # podría consultar ni borrar desde la web
        raise ValueError("El 'id' no puede contener '/' ni '\\' ni ser '.' o '..'.")
    return clean


def _decode_bytes(data: bytes) -> Optional[np.ndarray]:
    """
    Decodifica bytes de imagen a BGR. Primero con OpenCV (JPEG, PNG, WebP...) y, si falla,
    con Pillow, que además lee AVIF/HEIF (el dataset trae un AVIF con extensión .jpg).
    No se fía de la extensión del fichero, solo del contenido.

    Raises:
        ValueError: Si el fichero pesa demasiado o declara demasiados píxeles.
    """
    if not data:
        return None
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("La imagen es demasiado grande.")
    # Pillow lee solo la cabecera: así se rechaza una imagen gigante antes de decodificarla
    try:
        with Image.open(io.BytesIO(data)) as probe:
            width, height = probe.size
    except Image.DecompressionBombError:
        raise ValueError("La imagen tiene demasiados píxeles.")
    except Exception:
        width = height = 0  # formato que Pillow no reconoce: OpenCV aplica su propio límite
    if width * height > MAX_IMAGE_PIXELS:
        raise ValueError("La imagen tiene demasiados píxeles.")
    try:
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    except cv2.error:
        raise ValueError("La imagen tiene demasiados píxeles o no es válida.")
    if img is not None:
        # Última barrera por si OpenCV se importó antes de fijar su límite (ocurre en web/app.py fuera de Docker)
        if img.shape[0] * img.shape[1] > MAX_IMAGE_PIXELS:
            raise ValueError("La imagen tiene demasiados píxeles.")
        return img
    try:
        with Image.open(io.BytesIO(data)) as pil_img:
            rgb = np.asarray(pil_img.convert("RGB"))
        return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    except Exception:
        return None


def parse_input_image(foto: Union[str, Path, np.ndarray, bytes]) -> np.ndarray:
    """
    Convierte la foto (ruta, bytes o array de NumPy) en una imagen BGR uint8 (H, W, 3).

    Raises:
        ValueError: Si está vacía, no se puede decodificar o es demasiado grande.
        FileNotFoundError: Si la ruta no existe.
        TypeError: Si el tipo no es ninguno de los anteriores.
    """
    if foto is None:
        raise ValueError("El parámetro 'foto' no puede ser None.")

    if isinstance(foto, (str, Path)):
        file_path = str(foto)
        if not os.path.isfile(file_path):
            raise FileNotFoundError(f"No existe el archivo de imagen en la ruta: {file_path}")
        if os.path.getsize(file_path) > MAX_IMAGE_BYTES:
            raise ValueError("El archivo de imagen es demasiado grande.")

        with open(file_path, "rb") as f:
            data = f.read()
        img = _decode_bytes(data)
        if img is None:
            raise ValueError(f"El archivo existe pero no es una imagen válida o compatible: {file_path}")
        return img

    if isinstance(foto, (bytearray, memoryview)):
        foto = bytes(foto)
    if isinstance(foto, bytes):
        if len(foto) == 0:
            raise ValueError("El buffer de bytes de la imagen está vacío.")

        img = _decode_bytes(foto)
        if img is None:
            raise ValueError("No se pudieron decodificar los bytes a una imagen válida.")
        return img

    if isinstance(foto, np.ndarray):
        if foto.size == 0:
            raise ValueError("El array de imagen está vacío.")
        if foto.ndim in (2, 3) and foto.shape[0] * foto.shape[1] > MAX_IMAGE_PIXELS:
            raise ValueError("La imagen tiene demasiados píxeles.")
        if foto.dtype != np.uint8:
            # Arrays float/int/bool: se aceptan solo si son números finitos y se pasan a 0-255
            if not (np.issubdtype(foto.dtype, np.number) or foto.dtype == bool) or np.iscomplexobj(foto):
                raise ValueError(f"Tipo de píxel no soportado: {foto.dtype}")
            if not np.all(np.isfinite(foto)):
                raise ValueError("La imagen contiene valores NaN o infinitos.")
            foto = np.clip(foto, 0, 255).astype(np.uint8)

        if foto.ndim == 3 and foto.shape[2] == 1:
            foto = foto[:, :, 0]
        if foto.ndim == 2:
            return cv2.cvtColor(foto, cv2.COLOR_GRAY2BGR)

        if foto.ndim == 3:
            if foto.shape[2] == 4:
                return cv2.cvtColor(foto, cv2.COLOR_RGBA2BGR)
            # Con 3 canales se asume que ya está en BGR (como en OpenCV)
            if foto.shape[2] == 3:
                return foto.copy()
            raise ValueError(f"Número de canales no soportado: {foto.shape[2]}")

        raise ValueError(f"Dimensiones de matriz de imagen no válidas: shape={foto.shape}")

    raise TypeError(f"Tipo de dato no soportado para 'foto': {type(foto)}")


def parse_input_template(template: Union[np.ndarray, List[float], Tuple[float, ...], str]) -> np.ndarray:
    """
    Convierte el template (array, lista/tupla o texto con los valores separados por ';',
    ',' o espacios) en un vector float32 de 512 valores con norma 1.

    Raises:
        ValueError: Si no tiene 512 valores, contiene NaN/infinitos o es un vector de ceros.
        TypeError: Si el tipo no es ninguno de los anteriores.
    """
    if template is None:
        raise ValueError("El parámetro 'template' no puede ser None.")

    if isinstance(template, str):
        if len(template) > MAX_TEMPLATE_CHARS:
            raise ValueError("El template en texto es demasiado largo.")
        cleaned_str = template.strip("[]() \n\r")
        delimiter = ";" if ";" in cleaned_str else ("," if "," in cleaned_str else None)
        try:
            tokens = cleaned_str.split(delimiter) if delimiter else cleaned_str.split()
            arr = np.array([float(x) for x in tokens if x.strip()], dtype=np.float32)
        except Exception as err:
            raise ValueError(f"Fallo al parsear el template desde string: {err}")

    elif isinstance(template, (list, tuple)):
        if len(template) > EXPECTED_EMBEDDING_DIM:
            raise ValueError(
                f"Dimensión de template incorrecta: se esperaban {EXPECTED_EMBEDDING_DIM} valores, "
                f"pero se recibieron {len(template)}."
            )
        try:
            arr = np.array(template, dtype=np.float32).flatten()
        except Exception as err:
            raise ValueError(f"No se pudieron convertir los elementos de la lista a float: {err}")

    elif isinstance(template, np.ndarray):
        if template.size != EXPECTED_EMBEDDING_DIM:
            raise ValueError(
                f"Dimensión de template incorrecta: se esperaban {EXPECTED_EMBEDDING_DIM} valores, "
                f"pero se recibieron {template.size}."
            )
        if not (np.issubdtype(template.dtype, np.number) or template.dtype == bool) or np.iscomplexobj(template):
            raise ValueError(f"Tipo de dato no numérico en el template: {template.dtype}")
        arr = template.astype(np.float32).flatten()

    else:
        raise TypeError(f"Tipo de dato no soportado para 'template': {type(template)}")

    if arr.ndim != 1 or len(arr) != EXPECTED_EMBEDDING_DIM:
        raise ValueError(
            f"Dimensión de template incorrecta: se esperaban {EXPECTED_EMBEDDING_DIM} valores, "
            f"pero se recibieron {len(arr)}."
        )

    if not np.all(np.isfinite(arr)):
        raise ValueError("El template contiene valores no válidos (NaN o Infinito).")

    # La norma se calcula en float64: en float32, valores como 1e30 desbordan a infinito,
    # la división daba un vector de ceros y se llegaba a guardar en la BD como válido
    arr64 = arr.astype(np.float64)
    norm = float(np.linalg.norm(arr64))
    if not np.isfinite(norm) or norm == 0.0:
        raise ValueError("El template es un vector de ceros (norma nula).")

    return (arr64 / norm).astype(np.float32)


def average_templates(templates: List[np.ndarray]) -> np.ndarray:
    """
    Media de varios templates de la misma persona, normalizada a norma 1. Con fotos de
    distinta pose y luz reconoce mejor a la persona que el template de una sola foto.

    Raises:
        ValueError: Si la lista está vacía o algún template no es válido.
    """
    if not templates:
        raise ValueError("Se necesita al menos un template para promediar.")
    stacked = np.stack([parse_input_template(t) for t in templates])
    return parse_input_template(stacked.mean(axis=0))