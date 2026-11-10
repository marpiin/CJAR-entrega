"""
Paquete del sistema biométrico.

Fija el límite de píxeles de OpenCV aquí: OpenCV solo lee OPENCV_IO_MAX_IMAGE_PIXELS al IMPORTAR cv2
(cambiarla después no tiene efecto), y este fichero se ejecuta antes que cualquier módulo del paquete.
Eso solo sirve si `src` se importa antes que cv2: web/app.py importa cv2 (línea 35) ANTES que `src`
(línea 41), así que en la web este código llega tarde. En el contenedor lo cubre el
`ENV OPENCV_IO_MAX_IMAGE_PIXELS=40000000` del Dockerfile, que ya está definido cuando se importa cv2.
Fuera de Docker (python main.py, uvicorn a mano) la sonda de Pillow de src/utils.py (_decode_bytes)
rechaza los formatos que Pillow reconoce antes de decodificarlos; para los que no reconoce (HDR, PFM)
la última barrera es la comprobación de img.shape, que actúa DESPUÉS de reservar la imagen.
"""

import os

MAX_IMAGE_PIXELS = 40_000_000

_actual = os.environ.get("OPENCV_IO_MAX_IMAGE_PIXELS", "")
if not _actual.isdigit() or not 0 < int(_actual) <= MAX_IMAGE_PIXELS:
    os.environ["OPENCV_IO_MAX_IMAGE_PIXELS"] = str(MAX_IMAGE_PIXELS)
