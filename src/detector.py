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

# Al inscribir, una segunda cara con al menos esta fracción del área de la principal hace ambigua la foto
AREA_SEGUNDA_CARA = 0.5
# Lado mínimo (en píxeles de la foto) de la cara al inscribir: una plantilla de una cara diminuta no reconoce
# luego a su dueño. La cara más pequeña del dataset mide 84
LADO_MINIMO_INSCRIPCION = 64


class FaceDetector:
    def __init__(self, ctx_id: int = -1, det_size: tuple = (640, 640)):
        """ctx_id: -1 para CPU, 0 o más para la GPU con ese índice."""
        # Solo se carga el modelo de detección
        self.app = FaceAnalysis(name="buffalo_s", allowed_modules=["detection"])
        self.app.prepare(ctx_id=ctx_id, det_size=det_size)

    def detect_and_align(self, foto: Union[str, Path, np.ndarray, bytes], unica: bool = False) -> np.ndarray:
        """
        Devuelve la cara más grande de la foto alineada, en BGR y de 112x112x3.
        Lanza ValueError si no hay cara, si no se puede alinear o, con unica=True (inscripción), si
        otra cara mide al menos la mitad que la principal (no se sabría a quién se está inscribiendo) o la
        cara es demasiado pequeña.
        """
        img = parse_input_image(foto)
        faces = self.app.get(img)

        if not faces:
            raise ValueError("No se ha detectado ningún rostro en la imagen suministrada.")

        def area(f) -> float:
            return float((f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))

        faces = sorted(faces, key=area, reverse=True)
        primary_face = faces[0]
        if unica and len(faces) > 1 and area(faces[1]) >= AREA_SEGUNDA_CARA * area(primary_face):
            raise ValueError("Hay más de una cara de tamaño parecido: usa una foto con una sola persona.")

        x0, y0, x1, y1 = (float(v) for v in primary_face.bbox)
        if unica and min(x1 - x0, y1 - y0) < LADO_MINIMO_INSCRIPCION:
            raise ValueError("La cara es demasiado pequeña para inscribir: acércate o usa una foto mayor.")

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
