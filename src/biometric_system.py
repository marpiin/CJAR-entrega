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

Reglas de inscripción (los tres inscribir*):
- No se sustituye a una persona ya inscrita salvo reemplazar=True: si no, cualquiera podría inscribir
  su cara con el id de otro y suplantarlo.
- No se inscribe a quien se parezca a otra persona ya inscrita (similitud >= UMBRAL_PARECIDO) ni con un id
  que se confunda con otro, salvo permitir_parecido=True: evita alias de la misma persona, plantillas
  «lobo» (media de varias) y morphing con alguien inscrito. La excepción es para gemelos, y la decide el
  operador tras comprobar la identidad por otro medio.
- Con varias fotos, todas tienen que ser de la misma persona.

Reglas de decisión: umbral y margen respecto a las demás personas inscritas (src/matcher.py) y, si hay
imagen, que el parecido no dependa de ruido invisible (FeatureExtractor.estable).
"""

from typing import Optional, Union, List, Tuple
from pathlib import Path
import numpy as np

from src.detector import FaceDetector
from src.extractor import NORMA_INSCRIPCION, FeatureExtractor
from src.matcher import Matcher
from src.database import BiometricDatabase
from src.registro import fallo, limpiar, logger
from src.utils import parse_input_id, parse_input_template, average_templates

# Por debajo del umbral de verificación a propósito: la máxima similitud entre dos personas distintas de la
# BD oficial es 0,355 y la media de k plantillas de personas distintas da ~1/raíz(k) (0,41 con k=6)
UMBRAL_PARECIDO = 0.40
MAX_FOTOS_INSCRIPCION = 20


class BiometricSystem:
    def __init__(self, db_path: str = "database/database.csv", threshold: float = 0.48, crear_bd: bool = True):
        self.matcher = Matcher(threshold=threshold)
        self.db = BiometricDatabase(csv_path=db_path, crear=crear_bd)
        self.detector = FaceDetector()
        self.extractor = FeatureExtractor(proteger=self.db.protector.proteger)

    @staticmethod
    def _as_list(foto) -> list:
        """Permite pasar una foto suelta o una lista/tupla de fotos de la misma persona."""
        fotos = list(foto) if isinstance(foto, (list, tuple)) else [foto]
        if not 1 <= len(fotos) <= MAX_FOTOS_INSCRIPCION:
            raise ValueError(f"Se admiten entre 1 y {MAX_FOTOS_INSCRIPCION} fotos por inscripción.")
        return fotos

    # Operaciones comunes (lanzan excepciones; las usan los 9 métodos y la web)

    def coherencia(self, templates: List[np.ndarray]) -> List[float]:
        """Similitud de cada template con la media de los demás (lista vacía si solo hay uno)."""
        if len(templates) < 2:
            return []
        return [self.matcher.compute_similarity(t, average_templates(templates[:i] + templates[i + 1:]))
                for i, t in enumerate(templates)]

    def guardar(self, id: str, templates: List[np.ndarray], reemplazar: bool = False,
                permitir_parecido: bool = False) -> bool:
        """Aplica las reglas de inscripción y guarda la media de los templates."""
        if any(c < self.matcher.threshold for c in self.coherencia(templates)):
            # La media de dos personas distintas coincidiría con las dos: una identidad compartida
            raise ValueError("Las fotos no parecen de la misma persona.")
        return self.db.save_template(id, average_templates(templates), reemplazar=reemplazar,
                                     umbral_parecido=None if permitir_parecido else UMBRAL_PARECIDO)

    def decidir(self, id: str, template: np.ndarray, cara: Optional[np.ndarray] = None) -> bool:
        """Verificación 1:1. Con 'cara' (112x112) se comprueba además que el parecido sea estable."""
        try:
            id = parse_input_id(id)
        except (TypeError, ValueError):
            return False
        todos = self.db.get_all()
        # La estabilidad se calcula SIEMPRE (también si no hay coincidencia o el id no existe): si solo se
        # hiciera al coincidir, el tiempo de respuesta delataría que se está cerca del umbral
        estable = self._estable(cara, template, todos.get(id, template))
        return self._avisar(self.matcher.verify_against_all(id, template, todos, self.db.get_contrastes()), estable, id)

    def buscar(self, template: np.ndarray, cara: Optional[np.ndarray] = None) -> Optional[str]:
        """Identificación 1:N, con las mismas reglas."""
        todos = self.db.get_all()
        id, _ = self.matcher.identify_one_to_many(template, todos, self.db.get_contrastes())
        estable = self._estable(cara, template, todos[id] if id is not None else template)
        return id if self._avisar(id is not None, estable, id) else None

    def _estable(self, cara: Optional[np.ndarray], template: np.ndarray, inscrito: np.ndarray) -> bool:
        return cara is None or self.extractor.estable(cara, template, inscrito)

    @staticmethod
    def _avisar(coincide: bool, estable: bool, id: Optional[str]) -> bool:
        if coincide and not estable:
            logger.warning("Posible imagen adversaria: se parecía a «%s» pero el parecido desaparece al suavizarla", limpiar(id))
        return coincide and estable

    def registrar_contraste(self, id: str, foto: Union[str, Path, np.ndarray, bytes]) -> bool:
        """
        Para gemelos y dobles: registra la foto de alguien que se confunde con 'id' sin ser 'id'. No inscribe
        a nadie; a partir de ahí, una foto que se parezca más (o casi igual) al gemelo no se acepta como 'id'.
        """
        try:
            t = self.extractor.extract(self.detector.detect_and_align(foto, unica=True))
            return self.db.save_contraste(id, t)
        except Exception as exc:
            fallo("BiometricSystem.registrar_contraste", exc)
            return False

    # Flujo completo

    def inscribir(self, id: str, foto: Union[str, Path, np.ndarray, bytes, List], reemplazar: bool = False,
                  permitir_parecido: bool = False) -> bool:
        """
        Si 'foto' es una lista, se guarda la media de los templates de todas las fotos;
        si alguna no es válida (p. ej. sin cara) o no son de la misma persona no se inscribe nada.

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
            >>> s.inscribir("anita", "ana.jpg")                  # la misma cara con otro id
            False
        """
        try:
            templates = [
                self.extractor.extract(self.detector.detect_and_align(f, unica=True), NORMA_INSCRIPCION)
                for f in self._as_list(foto)
            ]
            return self.guardar(id, templates, reemplazar, permitir_parecido)
        except Exception as exc:
            fallo("BiometricSystem.inscribir", exc)
            return False

    def verificar(self, id: str, foto: Union[str, Path, np.ndarray, bytes]) -> bool:
        try:
            aligned_face = self.detector.detect_and_align(foto)
            query_template = self.extractor.extract(aligned_face)
            return self.decidir(id, query_template, aligned_face)
        except Exception as exc:
            fallo("BiometricSystem.verificar", exc)
            return False

    def identificar(self, foto: Union[str, Path, np.ndarray, bytes]) -> Optional[str]:
        try:
            aligned_face = self.detector.detect_and_align(foto)
            query_template = self.extractor.extract(aligned_face)
            return self.buscar(query_template, aligned_face)
        except Exception as exc:
            fallo("BiometricSystem.identificar", exc)
            return None

    # Sin detector (_sd)

    def inscribir_sd(self, id: str, foto: Union[str, Path, np.ndarray, bytes, List], reemplazar: bool = False,
                  permitir_parecido: bool = False) -> bool:
        """
        Como inscribir(), 'foto' puede ser una lista de fotos cuyos templates se promedian, y
        devuelve False si el id ya existe salvo reemplazar=True (ver inscribir()).
        """
        try:
            templates = [self.extractor.extract(f, NORMA_INSCRIPCION) for f in self._as_list(foto)]
            return self.guardar(id, templates, reemplazar, permitir_parecido)
        except Exception as exc:
            fallo("BiometricSystem.inscribir_sd", exc)
            return False

    def verificar_sd(self, id: str, foto: Union[str, Path, np.ndarray, bytes]) -> bool:
        try:
            aligned_face = self.extractor.preparar(foto)
            query_template = self.extractor.extract(aligned_face)
            return self.decidir(id, query_template, aligned_face)
        except Exception as exc:
            fallo("BiometricSystem.verificar_sd", exc)
            return False

    def identificar_sd(self, foto: Union[str, Path, np.ndarray, bytes]) -> Optional[str]:
        try:
            aligned_face = self.extractor.preparar(foto)
            query_template = self.extractor.extract(aligned_face)
            return self.buscar(query_template, aligned_face)
        except Exception as exc:
            fallo("BiometricSystem.identificar_sd", exc)
            return None

    # Sin detector ni extractor (_t)

    def inscribir_t(self, id: str, template: Union[np.ndarray, List[float], Tuple[float, ...], str],
                    reemplazar: bool = False, permitir_parecido: bool = False) -> bool:
        """
        Inscribe un template de 512 valores. Devuelve False si el id ya existe salvo
        reemplazar=True (ver inscribir()): así no se puede pisar la plantilla de otra persona.
        """
        try:
            valid_template = parse_input_template(template)
            return self.guardar(id, [valid_template], reemplazar, permitir_parecido)
        except Exception as exc:
            fallo("BiometricSystem.inscribir_t", exc)
            return False

    def verificar_t(self, id: str, template: Union[np.ndarray, List[float], Tuple[float, ...], str]) -> bool:
        try:
            valid_query_template = parse_input_template(template)
            return self.decidir(id, valid_query_template)
        except Exception as exc:
            fallo("BiometricSystem.verificar_t", exc)
            return False

    def identificar_t(self, template: Union[np.ndarray, List[float], Tuple[float, ...], str]) -> Optional[str]:
        try:
            valid_query_template = parse_input_template(template)
            return self.buscar(valid_query_template)
        except Exception as exc:
            fallo("BiometricSystem.identificar_t", exc)
            return None
