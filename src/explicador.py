"""
Explica en qué zonas de la cara se parecen (o se diferencian) dos personas.

Los 512 valores del template de ArcFace no tienen significado por separado, así que no
se puede leer "dimensión 37 = nariz". En su lugar se usa análisis por oclusión, que solo
necesita el extractor:
1. Se calcula la similitud base entre la cara A y el template de B.
2. Se tapa una zona de la cara A (con su color medio), se vuelve a extraer el template
   y se calcula de nuevo la similitud.
3. Aporte de la zona = similitud base - similitud con la zona tapada.
     - Aporte > 0: al quitar la zona baja el parecido, así que ahí se parecen.
     - Aporte < 0: al quitar la zona sube el parecido, así que esa zona los diferencia.
4. Si también se tiene la cara B, se repite tapando B frente al template de A y se
   promedian ambos sentidos.
Además de las zonas con nombre (ojos, nariz, boca...), una ventana que recorre toda la
cara genera un mapa de calor: verde donde se apoya el parecido y rojo donde lo resta.

Las zonas están definidas sobre la plantilla de alineamiento de ArcFace (112x112), así
que solo son exactas con caras alineadas por el detector (flujo completo).
"""

from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np

from src.extractor import FeatureExtractor
from src.utils import parse_input_template

# Zonas (x0, y0, x1, y1) en la cara alineada de 112x112. Referencia: en la plantilla de
# ArcFace los ojos caen en y≈51, la punta de la nariz en (56, 72) y la boca en y≈92.
# Izquierda/derecha según se ve en la foto (no según la persona).
REGIONES: Dict[str, Tuple[int, int, int, int]] = {
    "Frente": (18, 0, 94, 34),
    "Ojo izquierdo": (18, 34, 56, 64),
    "Ojo derecho": (56, 34, 94, 64),
    "Nariz": (42, 56, 70, 84),
    "Mejilla izquierda": (4, 62, 40, 96),
    "Mejilla derecha": (72, 62, 108, 96),
    "Boca": (32, 82, 80, 104),
    "Mentón": (30, 100, 82, 112),
}

FACE_SIZE = 112


class Explicador:
    def __init__(self, extractor: FeatureExtractor, ventana: int = 24, paso: int = 11):
        """
        Args:
            extractor: Extractor ArcFace ya cargado (se reutiliza, no se carga otro modelo).
            ventana: Lado en píxeles del cuadrado que recorre la cara para el mapa de calor.
            paso: Desplazamiento de la ventana entre posiciones.
        """
        self.extractor = extractor
        posiciones = list(range(0, FACE_SIZE - ventana + 1, paso))
        if posiciones[-1] != FACE_SIZE - ventana:
            posiciones.append(FACE_SIZE - ventana)  # que la ventana llegue al borde
        self.ventanas = [(x, y, x + ventana, y + ventana) for y in posiciones for x in posiciones]

    @staticmethod
    def _ocluir(face: np.ndarray, caja: Tuple[int, int, int, int], relleno: np.ndarray) -> np.ndarray:
        x0, y0, x1, y1 = caja
        out = face.copy()
        out[y0:y1, x0:x1] = relleno
        return out

    def _aportes(self, face: np.ndarray, t_ref: np.ndarray) -> Tuple[float, np.ndarray, np.ndarray]:
        """
        Tapa cada zona y cada ventana de 'face' y mide cuánto cae la similitud con t_ref.
        Todas las variantes se pasan por la red en un único lote.

        Returns:
            (similitud base, aporte de cada zona de REGIONES, mapa de aportes 112x112)
        """
        relleno = face.reshape(-1, 3).mean(axis=0).astype(np.uint8)
        cajas = list(REGIONES.values()) + self.ventanas
        variantes = [face] + [self._ocluir(face, c, relleno) for c in cajas]
        sims = self.extractor.extract_many(variantes) @ t_ref
        base = float(sims[0])
        caidas = base - sims[1:]

        n = len(REGIONES)
        suma = np.zeros((FACE_SIZE, FACE_SIZE), np.float32)
        cuenta = np.zeros_like(suma)
        for (x0, y0, x1, y1), c in zip(self.ventanas, caidas[n:]):
            suma[y0:y1, x0:x1] += c
            cuenta[y0:y1, x0:x1] += 1
        return base, caidas[:n], suma / np.maximum(cuenta, 1)

    @staticmethod
    def _pintar_mapa(face: np.ndarray, mapa: np.ndarray, escala: float) -> np.ndarray:
        """Superpone el mapa a la cara: verde = se parecen, rojo = se diferencian."""
        mapa = cv2.GaussianBlur(mapa, (0, 0), 4)
        alpha = np.clip(np.abs(mapa) / escala, 0, 1)[..., None] * 0.65
        color = np.where(mapa[..., None] >= 0, [80, 230, 0], [80, 40, 255]).astype(np.float32)  # BGR
        out = face.astype(np.float32) * (1 - alpha) + color * alpha
        out = np.clip(out, 0, 255).astype(np.uint8)
        return cv2.resize(out, (224, 224), interpolation=cv2.INTER_CUBIC)

    def explicar(
        self,
        cara_a: np.ndarray,
        t_b,
        cara_b: Optional[np.ndarray] = None,
        t_a=None,
    ) -> dict:
        """
        Explica la similitud entre la cara A y la persona B.

        Args:
            cara_a: Cara 112x112 BGR (la de la foto que se está comprobando).
            t_b: Template de la persona con la que se compara (p. ej. el de la BD).
            cara_b: Cara 112x112 de B, si se tiene. Con ella la explicación es simétrica.
            t_a: Template de A (si no se da y hay cara_b, se calcula).

        Returns:
            dict con:
              - "regiones": lista de {"nombre", "aporte", "caja"} ordenada de más a menos
                aporte (aporte = puntos de similitud que se pierden al tapar esa zona).
              - "mapas": [mapa de A, mapa de B o None] como imágenes BGR 224x224.
        """
        t_b = parse_input_template(t_b)
        cara_a = cv2.resize(cara_a, (FACE_SIZE, FACE_SIZE), interpolation=cv2.INTER_AREA)
        _, aporte, mapa_a = self._aportes(cara_a, t_b)
        mapa_b = None

        if cara_b is not None:
            cara_b = cv2.resize(cara_b, (FACE_SIZE, FACE_SIZE), interpolation=cv2.INTER_AREA)
            t_a = parse_input_template(t_a) if t_a is not None else self.extractor.extract(cara_a)
            _, aporte_b, mapa_b = self._aportes(cara_b, t_a)
            aporte = (aporte + aporte_b) / 2

        # Misma escala de color en las dos caras para que sean comparables. El mínimo evita
        # que diferencias minúsculas (p. ej. entre dos desconocidos) se pinten muy intensas.
        escala = max(float(np.abs(mapa_a).max()), float(np.abs(mapa_b).max()) if mapa_b is not None else 0.0, 0.03)
        regiones: List[dict] = [
            {"nombre": nombre, "aporte": float(a), "caja": list(caja)}
            for (nombre, caja), a in zip(REGIONES.items(), aporte)
        ]
        regiones.sort(key=lambda r: r["aporte"], reverse=True)
        return {
            "regiones": regiones,
            "mapas": [
                self._pintar_mapa(cara_a, mapa_a, escala),
                self._pintar_mapa(cara_b, mapa_b, escala) if mapa_b is not None else None,
            ],
        }
