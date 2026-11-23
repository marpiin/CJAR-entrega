"""
BD de plantillas en un CSV con columnas id,template.

Cada plantilla va cifrada y autenticada junto con su id y la última línea es un sello del fichero
entero (ver src/cripto.py). Leer el CSV no da ninguna plantilla utilizable; y si alguien lo modifica
sin la clave (añadir, cambiar, mover, duplicar o borrar filas, truncarlo o borrarlo), la lectura lanza
DatabaseReadError y el sistema FALLA CERRADO: no reconoce ni inscribe a nadie hasta que el operador
restaure una copia. Es una decisión consciente: con escritura sobre el fichero la denegación de servicio
es inevitable (basta con borrarlo), pero nunca debe convertirse en una suplantación.

Las escrituras (leer, modificar y reescribir) se serializan con un lock entre hilos y otro entre
procesos (flock sobre database.csv.lock); el temporal tiene un nombre único creado en exclusiva.

Al inscribir se comprueba que el id no existe (salvo reemplazar=True), que no se confunde con otro
(«Adam_Pearson», o «adam_pearson» con una letra cirílica) y, si se pide, que la cara no se parece a la
de otra persona ya inscrita.

Contrastes (para gemelos): filas «~id~n» con la cara de alguien que se parece a 'id' sin ser esa persona. No
son identidades (no salen en get_all() ni se identifican): solo compiten al decidir (src/matcher.py).
"""

import csv
import io
import os
import tempfile
import threading
import unicodedata
from contextlib import contextmanager
from typing import Dict, Optional, Tuple

import numpy as np

from src.cripto import IntegridadError, Protector
from src.registro import avisar_si_no_escribible, limpiar, logger
from src.utils import parse_input_id, parse_input_template

try:
    import fcntl
except ImportError:  # Windows sin Docker: solo queda el lock entre hilos
    fcntl = None

# Un registro ocupa unos 3 KB: 64 MB son más de 20.000 personas
MAX_DB_BYTES = 64 * 1024 * 1024
MAX_DB_ROWS = 100_000
CABECERA = b"id,template"
# Un contraste más parecido que esto a su persona sería probablemente una foto de ella misma
MAX_PARECIDO_CONTRASTE = 0.60


class DatabaseReadError(RuntimeError):
    """El CSV no se puede leer o no es auténtico: no se decide nada ni se escribe encima."""


class DatabaseCapacityError(RuntimeError):
    """La escritura dejaría el CSV por encima de MAX_DB_ROWS o MAX_DB_BYTES, que la lectura rechaza."""


class InscripcionRechazada(ValueError):
    """La persona (o un id que se confunde con el suyo) ya está inscrita."""


# Letras de otros alfabetos que se dibujan como una latina (cirílico, griego y variantes), por código
_PARECIDAS = {
    0x0430: "a", 0x0435: "e", 0x043E: "o", 0x0440: "p", 0x0441: "c", 0x0443: "y", 0x0445: "x", 0x0456: "i",
    0x0458: "j", 0x0455: "s", 0x0501: "d", 0x051B: "q", 0x051D: "w", 0x04BB: "h", 0x04CF: "l", 0x043A: "k",
    0x043C: "m", 0x043D: "h", 0x0442: "t", 0x0432: "b", 0x03B1: "a", 0x03BF: "o", 0x03C1: "p", 0x03BD: "v",
    0x03B9: "i", 0x03C4: "t", 0x03C5: "u", 0x03BA: "k", 0x03C7: "x", 0x03B5: "e", 0x03B7: "n", 0x0131: "i",
    0x0261: "g",
}


def alfabeto_claro(uid: str) -> bool:
    """
    False si el id mezcla letras latinas normales (a-z, tildes y demás de los bloques Latin-1, Extended-A/B y
    Additional) con letras de otro alfabeto, variantes fonéticas o letras modificadoras: no hay ningún nombre
    así y es la forma de imitar el de otra persona.
    """
    letras = [c for c in unicodedata.normalize("NFKC", uid) if unicodedata.category(c)[0] in "LM"]
    latinas = [c for c in letras if unicodedata.category(c) in ("Lu", "Ll", "Mn")
               and (ord(c) < 0x250 or 0x1E00 <= ord(c) < 0x1F00 or unicodedata.category(c) == "Mn")]
    if any(unicodedata.category(c) in ("Lm", "Sk") and ord(c) < 0x3000 for c in uid):
        return False
    raras = any(unicodedata.name(c, "").startswith("LATIN") for c in letras if c not in latinas)
    return not raras and (len(latinas) == len(letras) or not any(unicodedata.category(c)[0] == "L" for c in latinas))


def esqueleto(uid: str) -> str:
    """
    Forma reducida de un id: dos ids con el mismo esqueleto se confunden a simple vista. Se ignoran
    mayúsculas, tildes, formas de ancho completo, separadores y letras de otro alfabeto iguales a una latina.
    """
    t = unicodedata.normalize("NFKC", uid).casefold().translate(_PARECIDAS)
    t = "".join(c for c in unicodedata.normalize("NFD", t) if not 0x300 <= ord(c) < 0x370)  # tildes; no vocales indias
    letras = "".join(c for c in t if c.isalnum() or unicodedata.category(c)[0] == "M")
    # Cifras que imitan letras (a1ice, 4lice), salvo las del final (juan5 y juans son ids distintos)
    nombre = letras.rstrip("0123456789")
    return (nombre.translate(str.maketrans("014358", "oiaesb")).replace("l", "i") + letras[len(nombre):]) or t


class BiometricDatabase:
    def __init__(self, csv_path: str = "database/database.csv", clave: Optional[bytes] = None, crear: bool = True):
        """
        clave: 32 bytes (por defecto, la de src.cripto.cargar_clave()). crear=False: si el CSV no existe no
        se crea y las lecturas fallan (la web: un CSV borrado no puede pasar por una BD vacía).
        """
        self.csv_path = csv_path
        self.protector = Protector(clave)
        self._lock = threading.RLock()
        self._cache_lock = threading.Lock()
        self._cache: Optional[tuple] = None      # (huella, {id: plantilla})
        self._cache_raw: Optional[tuple] = None  # (huella, {id: texto cifrado}, contador)
        self._max_contador = 0
        self._ultimo_error = None
        directorio = os.path.dirname(self.csv_path) or "."
        if crear:
            os.makedirs(directorio, exist_ok=True)
        # Solo avisa: si el directorio no es escribible, inscribir y borrar fallarán (con este aviso se sabe por qué)
        avisar_si_no_escribible(os.path.abspath(directorio), "inscribir ni borrar personas")
        if crear:
            self._initialize_db()

    def _initialize_db(self) -> None:
        """Crea el CSV vacío y sellado si no existe. Creación EXCLUSIVA: nunca pisa un fichero que aparezca a la vez."""
        try:
            with open(self.csv_path, mode="xb") as f:
                f.write(CABECERA + b"\r\n" + self.protector.sellar([CABECERA], 0) + b"\r\n")
        except FileExistsError:
            pass

    # Escritura

    def save_template(self, user_id: str, template: np.ndarray, reemplazar: bool = False,
                      umbral_parecido: Optional[float] = None) -> bool:
        """
        Inserta a la persona. False si el id ya existe y no se pide reemplazar.

        Con umbral_parecido, lanza InscripcionRechazada si la plantilla se parece a la de OTRA persona
        (similitud >= umbral_parecido) o si el id se confunde con el de otra. La comprobación y la escritura
        van dentro del mismo lock: dos inscripciones simultáneas no pueden colarse la una a la otra.
        """
        uid = parse_input_id(user_id)
        plantilla = parse_input_template(template)
        with self._bloqueo():
            registros, contador = self._load()
            if reemplazar and uid in registros:
                registros = self._sin_contrastes(registros, uid)  # eran contrastes de la plantilla anterior
            if uid in registros and not reemplazar:
                return False
            if umbral_parecido is not None:
                self._comprobar_parecidos(uid, plantilla, registros, float(umbral_parecido))
            registros[uid] = self.protector.cifrar(uid, plantilla)
            return self._guardar(uid, registros, contador)

    def _guardar(self, uid: str, registros: Dict[str, str], contador: int) -> bool:
        try:
            self._write_all_records(registros, contador + 1)
        except DatabaseCapacityError as err:
            logger.warning("Base de datos '%s': no se inscribe a «%s»: %s", self.csv_path, limpiar(uid), err)
            return False
        return True

    def save_contraste(self, user_id: str, template: np.ndarray) -> bool:
        """
        Registra la cara de alguien que se confunde con 'user_id' (su gemelo). Lanza InscripcionRechazada si
        se parece tanto que podría ser la propia persona: dejaría de reconocérsela.
        """
        uid = parse_input_id(user_id)
        plantilla = parse_input_template(template)
        with self._bloqueo():
            registros, contador = self._load()
            propia = self.get_all().get(uid)
            if propia is None:
                return False
            if float(propia @ plantilla) >= MAX_PARECIDO_CONTRASTE:
                raise InscripcionRechazada(f"la cara se parece demasiado a «{uid}»: puede ser la misma persona")
            n = 1 + max((int(i.rsplit("~", 1)[1]) for i in registros if i.startswith(f"~{uid}~")), default=0)
            clave = f"~{uid}~{n}"
            registros[clave] = self.protector.cifrar(clave, plantilla)
            return self._guardar(clave, registros, contador)

    def delete_contrastes(self, user_id: str) -> bool:
        """Quita los contrastes de una persona. True si tenía alguno."""
        uid = parse_input_id(user_id)
        with self._bloqueo():
            registros, contador = self._load()
            quedan = self._sin_contrastes(registros, uid)
            if len(quedan) == len(registros):
                return False
            self._write_all_records(quedan, contador + 1)
        return True

    @staticmethod
    def _sin_contrastes(registros: Dict[str, str], uid: str) -> Dict[str, str]:
        return {i: t for i, t in registros.items() if not (i[0] == "~" and i[1:].rsplit("~", 1)[0] == uid)}

    @staticmethod
    def _id_de_fila(uid: str) -> bool:
        """True si 'uid' es un id de persona válido o el de un contraste («~id~n»)."""
        if uid[:1] != "~":
            return parse_input_id(uid) == uid
        persona, _, n = uid[1:].rpartition("~")
        return parse_input_id(persona) == persona and n.isascii() and n.isdigit() and len(n) <= 6

    def _comprobar_parecidos(self, uid: str, plantilla: np.ndarray, registros: Dict[str, str], umbral: float) -> None:
        if not alfabeto_claro(uid):
            raise InscripcionRechazada("el id mezcla alfabetos o usa letras que se confunden con otras")
        propio = esqueleto(uid)
        for otro in registros:
            if otro[0] != "~" and otro != uid and esqueleto(otro) == propio:
                raise InscripcionRechazada(f"el id se confunde con el de «{otro}», que ya está inscrito")
        # 'registros' ya no incluye los contrastes de la propia persona si se la está sustituyendo
        otros = {i: t for i, t in self._plantillas().items() if i != uid and i in registros}
        if otros:
            ids = list(otros)
            sims = np.stack([otros[i] for i in ids]) @ plantilla
            k = int(np.argmax(sims))
            if sims[k] >= umbral:
                que = f"a la de «{ids[k]}», que ya está inscrito" if ids[k][0] != "~" else "a la de un contraste (gemelo) registrado"
                raise InscripcionRechazada(f"la cara se parece demasiado {que}")

    def delete_template(self, user_id: str) -> bool:
        """True si la persona existía y se ha borrado."""
        try:
            uid = parse_input_id(user_id)
        except (TypeError, ValueError):
            return False
        with self._bloqueo():
            registros, contador = self._load()
            if uid not in registros:
                return False
            del registros[uid]
            registros = self._sin_contrastes(registros, uid)
            self._write_all_records(registros, contador + 1)
        return True

    # Lectura

    def existe(self, user_id) -> bool:
        try:
            uid = parse_input_id(user_id)
        except (TypeError, ValueError):
            return False
        return uid in self._load()[0]

    def get_template(self, user_id: str) -> Optional[np.ndarray]:
        """Plantilla de la persona, o None si no está o el id no es válido."""
        try:
            uid = parse_input_id(user_id)
        except (TypeError, ValueError):
            return None
        return self.get_all().get(uid)

    def get_all(self) -> Dict[str, np.ndarray]:
        """{id: plantilla} de las personas inscritas. Lanza DatabaseReadError si el fichero no es auténtico."""
        return {i: t for i, t in self._plantillas().items() if i[0] != "~"}

    def get_contrastes(self) -> Dict[str, np.ndarray]:
        """{~id~n: plantilla} de los contrastes (gemelos conocidos)."""
        return {i: t for i, t in self._plantillas().items() if i[0] == "~"}

    def _plantillas(self) -> Dict[str, np.ndarray]:
        """Todas las filas descifradas. Se guardan hasta que el fichero cambie."""
        huella = self._huella()
        with self._cache_lock:
            if huella is not None and self._cache and self._cache[0] == huella:
                return dict(self._cache[1])
        registros, _ = self._load()
        plantillas: Dict[str, np.ndarray] = {}
        for uid, texto in registros.items():
            try:
                t = parse_input_template(self.protector.descifrar(uid, texto))
            except ValueError as err:  # incluye IntegridadError: con el sello correcto no debería pasar
                raise self._error_lectura(f"la fila de «{limpiar(uid)}» no es auténtica") from err
            t.setflags(write=False)  # las copias del diccionario comparten el array: nadie puede corromper la caché
            plantillas[uid] = t
        if huella is not None and self._huella() == huella:
            with self._cache_lock:
                self._cache = (huella, plantillas)
        return dict(plantillas)

    def _huella(self):
        """Identifica la versión del fichero (se sustituye entero con os.replace)."""
        try:
            st = os.stat(self.csv_path)
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size, st.st_ino, st.st_ctime_ns)

    @contextmanager
    def _bloqueo(self):
        """Exclusión mutua para leer-modificar-escribir (entre hilos y entre procesos)."""
        with self._lock:
            if fcntl is None:
                yield
                return
            # Solo lectura basta para flock y funciona aunque el .lock lo creara otro usuario
            flags = os.O_RDONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(self.csv_path + ".lock", flags, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def _load(self) -> Tuple[Dict[str, str], int]:
        """Como _leer(), pero reutiliza la lectura mientras el fichero no cambie."""
        huella = self._huella()
        with self._cache_lock:
            if huella is not None and self._cache_raw and self._cache_raw[0] == huella:
                return dict(self._cache_raw[1]), self._cache_raw[2]
        registros, contador = self._leer()
        if huella is not None and self._huella() == huella:
            with self._cache_lock:
                self._cache_raw = (huella, registros, contador)
        return dict(registros), contador

    def _leer(self) -> Tuple[Dict[str, str], int]:
        """({id: texto cifrado}, contador). Lanza DatabaseReadError si falta, no se puede leer o no es auténtico."""
        # Se toma ANTES de abrir: si otro hilo escribe a mitad, leer la versión que había al empezar es correcto
        minimo = self._max_contador
        try:
            with open(self.csv_path, mode="rb") as f:
                datos = f.read(MAX_DB_BYTES + 1)  # nunca se carga entero un fichero enorme
        except FileNotFoundError as err:
            raise self._error_lectura("el fichero no existe") from err
        except OSError as err:
            raise self._error_lectura(f"no se puede leer: {limpiar(err)}") from err
        if len(datos) > MAX_DB_BYTES:
            raise self._error_lectura("es demasiado grande")
        lineas = datos.splitlines()
        if len(lineas) > MAX_DB_ROWS:
            raise self._error_lectura("tiene demasiadas filas")
        try:
            lineas, contador = self.protector.comprobar_sello(lineas)
        except IntegridadError as err:
            raise self._error_lectura(str(err)) from err
        if contador < minimo:
            raise self._error_lectura("es una versión anterior a la ya leída (copia antigua restaurada)")

        registros: Dict[str, str] = {}
        try:
            if not lineas or lineas[0] != CABECERA:
                raise ValueError("cabecera")
            for linea in lineas[1:]:
                uid, texto = next(csv.reader([linea.decode("utf-8")], strict=True))
                if not self._id_de_fila(uid) or uid in registros:
                    raise ValueError("id")
                registros[uid] = texto
        except (ValueError, TypeError, csv.Error, StopIteration) as err:
            raise self._error_lectura("el contenido sellado no tiene el formato esperado") from err
        self._max_contador = max(self._max_contador, contador)
        self._ultimo_error = None
        logger.info("Base de datos '%s': íntegra, %d personas (versión %d)", self.csv_path,
                    sum(i[0] != "~" for i in registros), contador)
        return registros, contador

    def _error_lectura(self, mensaje: str) -> DatabaseReadError:
        """Registra el fallo de lectura (una vez por versión del fichero, no por petición) y lo devuelve para lanzarlo."""
        clave = (self._huella(), mensaje)
        if clave != self._ultimo_error:
            self._ultimo_error = clave
            logger.warning("Base de datos '%s' RECHAZADA: %s. No se reconoce ni se inscribe a nadie hasta "
                           "restaurarla y reiniciar (git checkout database/database.csv).", self.csv_path, mensaje)
        return DatabaseReadError(f"Base de datos rechazada: {mensaje}")

    def _write_all_records(self, registros: Dict[str, str], contador: int) -> None:
        """
        Escribe en un fichero temporal y luego sustituye el original: así una
        interrupción a mitad nunca deja la BD vacía o truncada.
        """
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        for uid, texto in registros.items():
            writer.writerow([uid, texto])
        lineas = [CABECERA] + buf.getvalue().encode("utf-8").split(b"\n")[:-1]
        lineas.append(self.protector.sellar(lineas, contador))
        datos = b"\r\n".join(lineas) + b"\r\n"
        if len(lineas) > MAX_DB_ROWS or len(datos) > MAX_DB_BYTES:
            # Escribir un fichero que el propio lector rechaza dejaría la BD ilegible para siempre
            raise DatabaseCapacityError(
                f"se superarían los límites de la base de datos ({len(lineas)} filas y {len(datos)} bytes; "
                f"máximo {MAX_DB_ROWS} filas y {MAX_DB_BYTES} bytes)")
        # Nombre único creado en exclusiva (O_EXCL): no sigue enlaces ni choca con otro escritor
        directorio = os.path.dirname(os.path.abspath(self.csv_path))
        fd, tmp_path = tempfile.mkstemp(dir=directorio, prefix=".database-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(datos)
                f.flush()
                os.fsync(f.fileno())
            if hasattr(os, "chmod"):
                os.chmod(tmp_path, 0o644)
            os.replace(tmp_path, self.csv_path)
        except BaseException:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise
        self._max_contador = max(self._max_contador, contador)
