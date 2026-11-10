"""
Detector: localiza la cara principal de la foto con InsightFace (buffalo_s) y la alinea
a 112x112 con sus 5 puntos faciales, que es lo que espera el extractor.
"""

from typing import Union
from pathlib import Path
import numpy as np
from insightface.app import FaceAnalysis
from insightface.utils import face_align

from src.utils import parse_input_image


class FaceDetector:
    def __init__(self, ctx_id: int = -1, det_size: tuple = (640, 640)):
        """ctx_id: -1 para CPU, 0 o más para la GPU con ese índice."""
        # Solo se carga el modelo de detección
        self.app = FaceAnalysis(name="buffalo_s", allowed_modules=["detection"])
        self.app.prepare(ctx_id=ctx_id, det_size=det_size)

    def detect_and_align(self, foto: Union[str, Path, np.ndarray, bytes]) -> np.ndarray:
        """
        Devuelve la cara más grande de la foto alineada, en BGR y de 112x112x3.
        Lanza ValueError si no hay cara o no se puede alinear.
        """
        img = parse_input_image(foto)
        faces = self.app.get(img)

        if not faces:
            raise ValueError("No se ha detectado ningún rostro en la imagen suministrada.")

        primary_face = max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))

        # Alineamiento con los 5 landmarks (ojos, nariz, comisuras): transformación de
        # similitud a la plantilla estándar de ArcFace. Sin esto, un simple recorte del
        # bounding box da templates muy distintos para la misma persona con otra pose.
        kps = getattr(primary_face, "kps", None)
        if kps is None or not np.all(np.isfinite(kps)):
            raise ValueError("No se obtuvieron landmarks válidos para alinear el rostro.")

        aligned_face = face_align.norm_crop(img, landmark=kps, image_size=112)

        if aligned_face is None or aligned_face.size == 0:
            raise ValueError("No fue posible alinear el rostro detectado.")

        return aligned_face
