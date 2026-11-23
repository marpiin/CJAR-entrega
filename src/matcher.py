"""
Comparador: similitud coseno entre templates y decisión con el umbral y el margen.

- Umbral: la similitud con la persona tiene que ser >= umbral.
- Margen: además tiene que superar en 'margen' a la de cualquier OTRA persona inscrita. Si dos personas
  quedan casi empatadas (gemelos inscritos los dos, o una plantilla «lobo» fabricada como media de varias
  personas para coincidir con todas a la vez) no se decide por ninguna.
- Contrastes: caras de gemelos conocidos que no son identidades. Cuentan para el margen como una persona más,
  pero nunca se identifican.
"""

import math
from typing import Dict, Tuple, Optional
import numpy as np
from src.utils import parse_input_template


class Matcher:
    def __init__(self, threshold: float = 0.48, margen: float = 0.05):
        self.threshold = float(threshold)
        self.margen = float(margen)
        if not (math.isfinite(self.threshold) and 0.0 < self.threshold <= 1.0):
            raise ValueError("El umbral debe estar entre 0 (sin incluir) y 1.")
        if not (math.isfinite(self.margen) and 0.0 <= self.margen < 1.0):
            raise ValueError("El margen debe estar entre 0 y 1.")

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
        """Devuelve (coincide, similitud) mirando solo a esa persona (sin margen)."""
        score = self.compute_similarity(t_query, t_enrolled)
        return score >= self.threshold, score

    def _similitudes(self, t_query: np.ndarray, all_templates: Dict[str, np.ndarray]) -> Tuple[list, np.ndarray]:
        ids = list(all_templates)
        q = parse_input_template(t_query)
        return ids, np.clip(np.stack([all_templates[i] for i in ids]) @ q, -1.0, 1.0)

    def verify_against_all(self, user_id: str, t_query: np.ndarray, all_templates: Dict[str, np.ndarray],
                           contrastes: Optional[Dict[str, np.ndarray]] = None) -> bool:
        """
        Verificación 1:1 con control de ambigüedad: True solo si la similitud con 'user_id' llega al umbral
        y ninguna otra persona inscrita queda a menos de 'margen' de ella.
        """
        if user_id not in all_templates:
            return False
        ids, sims = self._similitudes(t_query, {**all_templates, **(contrastes or {})})
        k = ids.index(user_id)
        propia = float(sims[k])
        if propia < self.threshold:
            return False
        otras = np.delete(sims, k)
        return otras.size == 0 or propia - float(otras.max()) >= self.margen

    def identify_one_to_many(
        self, t_query: np.ndarray, all_templates: Dict[str, np.ndarray],
        contrastes: Optional[Dict[str, np.ndarray]] = None
    ) -> Tuple[Optional[str], float]:
        """
        Devuelve (id más parecido, su similitud). El id es None si la mejor similitud no llega al
        umbral o si otra persona queda a menos de 'margen'.
        """
        if not all_templates:
            return None, 0.0
        ids, sims = self._similitudes(t_query, {**all_templates, **(contrastes or {})})
        k = int(np.argmax(sims))
        mejor = float(sims[k])
        otras = np.delete(sims, k)
        if ids[k] not in all_templates or mejor < self.threshold or (otras.size and mejor - float(otras.max()) < self.margen):
            return None, mejor
        return ids[k], mejor
