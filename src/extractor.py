"""
Extractor: convierte una cara de 112x112 en un template de 512 valores con ArcFace
(modelo de buffalo_s) y lo normaliza a norma 1.
"""

from typing import Callable, List, Optional, Union
from pathlib import Path
import cv2
import numpy as np
from insightface.app import FaceAnalysis

from src.utils import parse_input_image, parse_input_template, EXPECTED_EMBEDDING_DIM

# Norma mínima del vector que saca la red antes de normalizarlo. En ArcFace esa norma mide lo clara que
# es la cara: en 13.186 caras de LFW la mínima fue 13,4 y en las del dataset 16,5; el ruido da 10, una
# imagen lisa 4-6 y las imágenes fabricadas a ciegas desde ruido 8-16. No frena a quien la optimice.
NORMA_MINIMA = 12.0
# Al inscribir se pide más: una plantilla mala no reconoce luego a su dueño (desenfoque, oscuridad, baja resolución)
NORMA_INSCRIPCION = 16.0

# Fracción del parecido que tiene que conservarse al suavizar la cara. Medido: en 1.057 parejas genuinas de
# LFW la mediana es 0,91 y en los ataques de ruido invisible que no conocen esta defensa se queda por debajo
# de 0,55. Un atacante que la conozca y la incluya en su optimización puede pasarla con más ruido.
ESTABILIDAD_MINIMA = 0.72


class FeatureExtractor:
    def __init__(self, ctx_id: int = -1, proteger: Optional[Callable[[np.ndarray], np.ndarray]] = None):
        """
        ctx_id: -1 para CPU, 0 o más para la GPU con ese índice.
        proteger: transformación secreta de las plantillas (Protector.proteger); sin ella salen las de ArcFace.
        """
        self._proteger = proteger or (lambda v: v)
        # FaceAnalysis exige cargar también el detector aunque aquí solo se use el reconocimiento
        self.app = FaceAnalysis(name="buffalo_s", allowed_modules=["detection", "recognition"])
        self.app.prepare(ctx_id=ctx_id)

        self.model = self.app.models.get("recognition")
        if self.model is None:
            raise RuntimeError("No se pudo inicializar el modelo de reconocimiento facial.")

    @staticmethod
    def preparar(face_img: Union[str, Path, np.ndarray, bytes]) -> np.ndarray:
        """Imagen BGR de 112x112 que entra en la red."""
        img = parse_input_image(face_img)
        if (img.shape[0], img.shape[1]) != (112, 112):
            img = cv2.resize(img, (112, 112), interpolation=cv2.INTER_AREA)
        return img

    def estable(self, face: np.ndarray, t_query: np.ndarray, t_ref: np.ndarray) -> bool:
        """
        True si el parecido con t_ref no se desploma al suavizar la cara. Una perturbación adversaria
        (ruido invisible calculado con los gradientes de la red) vive en el detalle fino y se borra con un
        filtro; el parecido de una cara real apenas cambia (ver ESTABILIDAD_MINIMA).
        """
        variantes = [cv2.GaussianBlur(face, (0, 0), 1.5), cv2.medianBlur(face, 5)]
        base = float(np.dot(t_query, t_ref))
        return float((self.extract_many(variantes) @ t_ref).min()) >= ESTABILIDAD_MINIMA * base

    def extract(self, face_img: Union[str, Path, np.ndarray, bytes], norma_minima: float = NORMA_MINIMA) -> np.ndarray:
        """
        Template de 512 valores de la cara. Si no mide 112x112 se redimensiona tal cual. Lanza ValueError si
        la imagen no tiene calidad de cara (lisa, ruido, sin recortar): sin detector no hay otro control.
        """
        img = self.preparar(face_img)
        raw_embedding = self.model.get_feat(img)

        if raw_embedding is None or raw_embedding.size == 0:
            raise ValueError("El extractor no pudo obtener características biométricas de la imagen.")

        norma = float(np.linalg.norm(raw_embedding))
        if not np.isfinite(norma) or norma < norma_minima:
            raise ValueError("La imagen no parece una cara alineada de 112x112 (calidad insuficiente).")

        return parse_input_template(self._proteger(raw_embedding.flatten()))

    def extract_many(self, faces: List[np.ndarray]) -> np.ndarray:
        """
        Extrae los templates de varias caras 112x112 BGR en una sola pasada de la red
        (mucho más rápido que llamar a extract() en bucle). Devuelve una matriz (N, 512)
        con cada fila normalizada a norma 1.
        """
        if not faces:
            return np.empty((0, EXPECTED_EMBEDDING_DIM), dtype=np.float32)
        raw = np.asarray(self.model.get_feat(list(faces)), dtype=np.float32).reshape(len(faces), -1)
        return np.stack([parse_input_template(r) for r in self._proteger(raw)])
