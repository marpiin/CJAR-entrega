"""
Clase principal con los 9 métodos del enunciado.

- Flujo completo (foto -> detector -> extractor -> comparador): inscribir, verificar, identificar.
- _sd, sin detector (la foto va directa al extractor): inscribir_sd, verificar_sd, identificar_sd.
- _t, sin detector ni extractor (se pasa el template): inscribir_t, verificar_t, identificar_t.

Ningún método lanza excepciones: ante cualquier error devuelven False (inscribir y verificar)
o None (identificar). Pero el fallo no queda en silencio: se registra un WARNING en el logger
"cjar" con el método, el tipo de excepción y el mensaje de la excepción tratado por limpiar()
(una línea, sin controles, sin lo entrecomillado ni decimales; NO garantiza que no salgan enteros,
rutas u otro texto: ver src/registro.py). Un resultado normal (id inexistente, sin coincidencia)
no es un fallo y no se registra.

Los inscribir* no sustituyen a una persona ya inscrita (devuelven False) salvo que se pase
reemplazar=True: si no, cualquiera podría inscribir su cara con el id de otro y suplantarlo.
"""

from typing import Optional, Union, List, Tuple
from pathlib import Path
import numpy as np

from src.detector import FaceDetector
from src.extractor import FeatureExtractor
from src.matcher import Matcher
from src.database import BiometricDatabase
from src.registro import fallo
from src.utils import parse_input_template, average_templates


class BiometricSystem:
    def __init__(self, db_path: str = "database/database.csv", threshold: float = 0.48):
        self.detector = FaceDetector()
        self.extractor = FeatureExtractor()
        self.matcher = Matcher(threshold=threshold)
        self.db = BiometricDatabase(csv_path=db_path)

    @staticmethod
    def _as_list(foto) -> list:
        """Permite pasar una foto suelta o una lista/tupla de fotos de la misma persona."""
        return list(foto) if isinstance(foto, (list, tuple)) else [foto]

    # Flujo completo

    def inscribir(self, id: str, foto: Union[str, Path, np.ndarray, bytes, List], reemplazar: bool = False) -> bool:
        """
        Si 'foto' es una lista, se guarda la media de los templates de todas las fotos;
        si alguna no es válida (p. ej. sin cara) no se inscribe nada.

        Devuelve False si el id YA EXISTE, salvo reemplazar=True. Es una decisión consciente: si
        reinscribir sustituyera la plantilla, cualquiera podría inscribir su cara con el id de
        otra persona y suplantarla. False también significa cualquier otro fallo (el motivo queda
        en el log, logger "cjar").

            >>> s.inscribir("ana", "ana.jpg")                    # id nuevo
            True
            >>> s.inscribir("ana", "otra.jpg")                   # ya existe: no se sustituye
            False
            >>> s.inscribir("ana", "otra.jpg", reemplazar=True)  # sustituir a propósito
            True
        """
        try:
            templates = [
                self.extractor.extract(self.detector.detect_and_align(f))
                for f in self._as_list(foto)
            ]
            return self.db.save_template(id, average_templates(templates), reemplazar=reemplazar)
        except Exception as exc:
            fallo("BiometricSystem.inscribir", exc)
            return False

    def verificar(self, id: str, foto: Union[str, Path, np.ndarray, bytes]) -> bool:
        try:
            enrolled_template = self.db.get_template(id)
            if enrolled_template is None:
                return False

            aligned_face = self.detector.detect_and_align(foto)
            query_template = self.extractor.extract(aligned_face)
            is_match, _ = self.matcher.verify_one_to_one(query_template, enrolled_template)
            return is_match
        except Exception as exc:
            fallo("BiometricSystem.verificar", exc)
            return False

    def identificar(self, foto: Union[str, Path, np.ndarray, bytes]) -> Optional[str]:
        try:
            aligned_face = self.detector.detect_and_align(foto)
            query_template = self.extractor.extract(aligned_face)
            all_templates = self.db.get_all()
            matched_id, _ = self.matcher.identify_one_to_many(query_template, all_templates)
            return matched_id
        except Exception as exc:
            fallo("BiometricSystem.identificar", exc)
            return None

    # Sin detector (_sd)

    def inscribir_sd(self, id: str, foto: Union[str, Path, np.ndarray, bytes, List], reemplazar: bool = False) -> bool:
        """
        Como inscribir(), 'foto' puede ser una lista de fotos cuyos templates se promedian, y
        devuelve False si el id ya existe salvo reemplazar=True (ver inscribir()).
        """
        try:
            templates = [self.extractor.extract(f) for f in self._as_list(foto)]
            return self.db.save_template(id, average_templates(templates), reemplazar=reemplazar)
        except Exception as exc:
            fallo("BiometricSystem.inscribir_sd", exc)
            return False

    def verificar_sd(self, id: str, foto: Union[str, Path, np.ndarray, bytes]) -> bool:
        try:
            enrolled_template = self.db.get_template(id)
            if enrolled_template is None:
                return False

            query_template = self.extractor.extract(foto)
            is_match, _ = self.matcher.verify_one_to_one(query_template, enrolled_template)
            return is_match
        except Exception as exc:
            fallo("BiometricSystem.verificar_sd", exc)
            return False

    def identificar_sd(self, foto: Union[str, Path, np.ndarray, bytes]) -> Optional[str]:
        try:
            query_template = self.extractor.extract(foto)
            all_templates = self.db.get_all()
            matched_id, _ = self.matcher.identify_one_to_many(query_template, all_templates)
            return matched_id
        except Exception as exc:
            fallo("BiometricSystem.identificar_sd", exc)
            return None

    # Sin detector ni extractor (_t)

    def inscribir_t(self, id: str, template: Union[np.ndarray, List[float], Tuple[float, ...], str],
                    reemplazar: bool = False) -> bool:
        """
        Inscribe un template de 512 valores. Devuelve False si el id ya existe salvo
        reemplazar=True (ver inscribir()): así no se puede pisar la plantilla de otra persona.
        """
        try:
            valid_template = parse_input_template(template)
            return self.db.save_template(id, valid_template, reemplazar=reemplazar)
        except Exception as exc:
            fallo("BiometricSystem.inscribir_t", exc)
            return False

    def verificar_t(self, id: str, template: Union[np.ndarray, List[float], Tuple[float, ...], str]) -> bool:
        try:
            enrolled_template = self.db.get_template(id)
            if enrolled_template is None:
                return False

            valid_query_template = parse_input_template(template)
            is_match, _ = self.matcher.verify_one_to_one(valid_query_template, enrolled_template)
            return is_match
        except Exception as exc:
            fallo("BiometricSystem.verificar_t", exc)
            return False

    def identificar_t(self, template: Union[np.ndarray, List[float], Tuple[float, ...], str]) -> Optional[str]:
        try:
            valid_query_template = parse_input_template(template)
            all_templates = self.db.get_all()
            matched_id, _ = self.matcher.identify_one_to_many(valid_query_template, all_templates)
            return matched_id
        except Exception as exc:
            fallo("BiometricSystem.identificar_t", exc)
            return None
