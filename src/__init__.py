"""
Paquete del sistema biométrico.

Fija el límite de píxeles de OpenCV aquí: OpenCV solo lee OPENCV_IO_MAX_IMAGE_PIXELS al IMPORTAR cv2
(cambiarla después no tiene efecto), y este fichero se ejecuta antes que cualquier módulo del paquete.
Eso solo sirve si `src` se importa antes que cv2: por eso web/app.py importa `src` primero. En el
contenedor lo cubre además el `ENV OPENCV_IO_MAX_IMAGE_PIXELS=40000000` del Dockerfile.
"""

import os

MAX_IMAGE_PIXELS = 40_000_000

_actual = os.environ.get("OPENCV_IO_MAX_IMAGE_PIXELS", "")
if not _actual.isdigit() or not 0 < int(_actual) <= MAX_IMAGE_PIXELS:
    os.environ["OPENCV_IO_MAX_IMAGE_PIXELS"] = str(MAX_IMAGE_PIXELS)
