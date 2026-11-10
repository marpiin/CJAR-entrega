"""
Comparador: similitud coseno entre templates y decisión con el umbral.
Si la similitud es >= umbral se considera la misma persona.
"""

from typing import Dict, Tuple, Optional
import numpy as np
from src.utils import parse_input_template

class Matcher:
    def __init__(self, threshold: float = 0.48):
        self.threshold = float(threshold)

    def compute_similarity(self, t1: np.ndarray, t2: np.ndarray) -> float:
        """
        Similitud coseno en [-1, 1]. Como los templates tienen norma 1, es directamente
        su producto escalar.
        """
        v1 = parse_input_template(t1)
        v2 = parse_input_template(t2)
        # El redondeo en float32 puede dar valores como 1.0000001
        return float(np.clip(np.dot(v1, v2), -1.0, 1.0))

    def verify_one_to_one(self, t_query: np.ndarray, t_enrolled: np.ndarray) -> Tuple[bool, float]:
        """Devuelve (coincide, similitud)."""
        score = self.compute_similarity(t_query, t_enrolled)
        return score >= self.threshold, score

    def identify_one_to_many(
        self, t_query: np.ndarray, all_templates: Dict[str, np.ndarray]
    ) -> Tuple[Optional[str], float]:
        """
        Devuelve (id más parecido, su similitud). El id es None si la mejor similitud
        no llega al umbral.
        """
        if not all_templates:
            return None, 0.0

        v_query = parse_input_template(t_query)
        best_id: Optional[str] = None
        best_score: float = -1.0

        for user_id, enrolled_vector in all_templates.items():
            try:
                score = self.compute_similarity(v_query, enrolled_vector)
            except Exception:
                continue  # template corrupto: se salta
            if score > best_score:
                best_score = score
                best_id = user_id

        if best_score >= self.threshold:
            return best_id, best_score

        return None, best_score
