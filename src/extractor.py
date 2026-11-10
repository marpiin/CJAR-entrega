"""
Extractor: convierte una cara de 112x112 en un template de 512 valores con ArcFace
(modelo de buffalo_s) y lo normaliza a norma 1.
"""

from typing import List, Union
from pathlib import Path
import cv2
import numpy as np
from insightface.app import FaceAnalysis

from src.utils import parse_input_image, parse_input_template, EXPECTED_EMBEDDING_DIM


class FeatureExtractor:
    def __init__(self, ctx_id: int = -1):
        """ctx_id: -1 para CPU, 0 o más para la GPU con ese índice."""
        # FaceAnalysis exige cargar también el detector aunque aquí solo se use el reconocimiento
        self.app = FaceAnalysis(name="buffalo_s", allowed_modules=["detection", "recognition"])
        self.app.prepare(ctx_id=ctx_id)

        self.model = self.app.models.get("recognition")
        if self.model is None:
            raise RuntimeError("No se pudo inicializar el modelo de reconocimiento facial.")

    def extract(self, face_img: Union[str, Path, np.ndarray, bytes]) -> np.ndarray:
        """Template de 512 valores de la cara. Si no mide 112x112 se redimensiona tal cual."""
        img = parse_input_image(face_img)

        if (img.shape[0], img.shape[1]) != (112, 112):
            img = cv2.resize(img, (112, 112), interpolation=cv2.INTER_AREA)

        raw_embedding = self.model.get_feat(img)

        if raw_embedding is None or raw_embedding.size == 0:
            raise ValueError("El extractor no pudo obtener características biométricas de la imagen.")

        return parse_input_template(raw_embedding.flatten())

    def extract_many(self, faces: List[np.ndarray]) -> np.ndarray:
        """
        Extrae los templates de varias caras 112x112 BGR en una sola pasada de la red
        (mucho más rápido que llamar a extract() en bucle). Devuelve una matriz (N, 512)
        con cada fila normalizada a norma 1.
        """
        if not faces:
            return np.empty((0, EXPECTED_EMBEDDING_DIM), dtype=np.float32)
        raw = np.asarray(self.model.get_feat(list(faces)), dtype=np.float32).reshape(len(faces), -1)
        return np.stack([parse_input_template(r) for r in raw])
