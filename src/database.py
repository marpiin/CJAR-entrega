"""
BD de templates en un CSV con columnas id,template (los 512 valores separados por ';').

Cada fila se valida al leerla: las que se hayan editado a mano con valores que no son
un template válido (letras, NaN, vectores incompletos, bytes que no son UTF-8, campos
enormes...) se ignoran y el resto sigue funcionando. Esas filas se conservan tal cual al
reescribir el fichero, para no perder datos que alguien pueda querer recuperar.

Si el fichero no se puede leer (permisos, bloqueado por otro programa, demasiado grande),
se lanza DatabaseReadError y no se escribe nada: antes se trataba como una BD vacía y la
siguiente inscripción la reescribía con un solo registro.

Las escrituras (leer, modificar y reescribir) se serializan con un lock entre hilos y otro
entre procesos (flock sobre database.csv.lock), para que la web y cargar_dataset.py no se
pisen; el fichero temporal tiene un nombre único y se crea en exclusiva. Inscribir un id que ya existe solo lo sustituye si se pide explícitamente
(reemplazar=True): así nadie puede quedarse en silencio con la identidad de otra persona.
"""

import csv
import io
import logging
import os
import tempfile
import threading
from contextlib import contextmanager
import numpy as np
from typing import Dict, List, Optional, Tuple
from src.registro import avisar_si_no_escribible, limpiar, logger
from src.utils import parse_input_id, parse_input_template

try:
    import fcntl
except ImportError:  # Windows sin Docker: solo queda el lock entre hilos
    fcntl = None

# Un registro ocupa unos 5 KB: 64 MB son más de 10.000 personas
MAX_DB_BYTES = 64 * 1024 * 1024
# Un CSV con millones de filas cortas cabe en 64 MB y cada consulta pública lo reprocesaría
MAX_DB_ROWS = 100_000


class DatabaseReadError(RuntimeError):
    """El CSV existe pero no se ha podido leer: no se debe escribir encima."""


class DatabaseCapacityError(RuntimeError):
    """La escritura dejaría el CSV por encima de MAX_DB_ROWS o MAX_DB_BYTES, que la lectura rechaza."""

class BiometricDatabase:
    def __init__(self, csv_path: str = "database/database.csv"):
        self.csv_path = csv_path
        self._lock = threading.RLock()
        self._cache_lock = threading.Lock()
        self._cache = None
        self._cache_raw = None
        self._ultimo_n = None        # personas en la lectura anterior de esta ejecución
        self._ultimo_informe = None  # para avisar una sola vez por cada cambio real del fichero
        self._ultimo_error = None
        os.makedirs(os.path.dirname(self.csv_path) or ".", exist_ok=True)
        # Solo avisa: si el directorio no es escribible, inscribir y borrar fallarán (con este aviso se sabe por qué)
        avisar_si_no_escribible(os.path.dirname(os.path.abspath(self.csv_path)), "inscribir ni borrar personas")
        self._initialize_db()

    def _initialize_db(self) -> None:
        """
        Crea el CSV con la cabecera si aún no existe. La creación es EXCLUSIVA (modo "x", O_EXCL): se llama
        también desde la lectura (_leer), que no toma el lock, y con "comprobar que falta y abrir con 'w'"
        una lectura podía truncar a solo la cabecera un CSV que otro proceso acababa de restaurar.
        """
        try:
            with open(self.csv_path, mode="x", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["id", "template"])
        except FileExistsError:
            pass  # ya existe (o apareció justo ahora): no se toca

    def save_template(self, user_id: str, template: np.ndarray, reemplazar: bool = False) -> bool:
        """
        Inserta la persona. Si ya existe, sustituye su template solo con reemplazar=True;
        si no, devuelve False sin tocar nada.
        """
        clean_user_id = parse_input_id(user_id)
        clean_template = parse_input_template(template)
        serialized_template = ";".join(f"{val:.7f}" for val in clean_template)

        with self._bloqueo():
            records, raros = self._load()
            if clean_user_id in records and not reemplazar:
                return False
            records[clean_user_id] = serialized_template
            try:
                self._write_all_records(records, raros)
            except DatabaseCapacityError as err:
                # Antes la escritura no aplicaba los topes de la lectura: una inscripción que los cruzaba dejaba
                # la BD ilegible (503 permanente, ni borrar servía). Ahora no se escribe nada y la BD sigue como estaba.
                logger.warning("Base de datos '%s': no se inscribe a «%s»: %s", self.csv_path, limpiar(clean_user_id), err)
                return False
            self._ultimo_n = len(records)
        return True

    def existe(self, user_id) -> bool:
        """True si el id está en el CSV (con plantilla válida o no)."""
        try:
            clean_user_id = parse_input_id(user_id)
        except (TypeError, ValueError):
            return False
        return clean_user_id in self._read_raw_records()

    def get_template(self, user_id: str) -> Optional[np.ndarray]:
        """Template de la persona, o None si no está, su fila no es válida o el id no lo es."""
        try:
            clean_user_id = parse_input_id(user_id)
        except (TypeError, ValueError):
            return None
        records = self._read_raw_records()

        if clean_user_id not in records:
            return None

        try:
            return parse_input_template(records[clean_user_id])
        except Exception:
            return None

    def _huella(self):
        """Identifica la versión del fichero (se sustituye entero con os.replace)."""
        try:
            st = os.stat(self.csv_path)
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size, st.st_ino)

    def get_all(self) -> Dict[str, np.ndarray]:
        """
        {id: template} con todas las filas válidas. El resultado se guarda hasta que el
        fichero cambie, para no revalidar todas las plantillas en cada consulta.
        """
        huella = self._huella()
        with self._cache_lock:
            if huella is not None and self._cache and self._cache[0] == huella:
                return dict(self._cache[1])
        records = self._read_raw_records()
        valid_templates: Dict[str, np.ndarray] = {}

        for uid, raw_vector_str in records.items():
            try:
                valid_templates[uid] = parse_input_template(raw_vector_str)
            except Exception:
                continue

        if huella is not None and self._huella() == huella:
            with self._cache_lock:
                self._cache = (huella, valid_templates)
        invalidas = sorted(set(records) - set(valid_templates))
        if invalidas:  # solo se llega aquí al calcular (no con la caché): un aviso por cambio del fichero
            logger.warning("Base de datos '%s': %d personas con una plantilla que no es válida (se ignoran): %s",
                           self.csv_path, len(invalidas), ", ".join(limpiar(i) for i in invalidas[:5]) + (" …" if len(invalidas) > 5 else ""))
        return dict(valid_templates)

    def delete_template(self, user_id: str) -> bool:
        """True si la persona existía y se ha borrado."""
        try:
            clean_user_id = parse_input_id(user_id)
        except (TypeError, ValueError):
            return False
        with self._bloqueo():
            records, raros = self._load()
            if clean_user_id not in records:
                return False
            del records[clean_user_id]
            self._write_all_records(records, raros)
            self._ultimo_n = len(records)  # borrar a propósito no es una bajada sospechosa
        return True

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

    def _read_raw_records(self) -> Dict[str, str]:
        """Pares {id: template en texto} sin validar."""
        return self._load()[0]

    def _load(self) -> Tuple[Dict[str, str], List[bytes]]:
        """
        Como _leer(), pero reutiliza la lectura mientras el fichero no cambie (huella):
        así verificar no reparsea todo el CSV en cada petición.
        """
        huella = self._huella()
        with self._cache_lock:
            if huella is not None and self._cache_raw and self._cache_raw[0] == huella:
                return dict(self._cache_raw[1]), list(self._cache_raw[2])
        records, raros = self._leer()
        if huella is not None and self._huella() == huella:
            with self._cache_lock:
                self._cache_raw = (huella, records, raros)
        return dict(records), list(raros)

    def _leer(self) -> Tuple[Dict[str, str], List[bytes]]:
        """
        Lee el CSV fila a fila. Devuelve ({id: template en texto}, filas que no se han
        entendido, en bruto). Raises DatabaseReadError si el fichero no se puede leer.
        """
        if not os.path.exists(self.csv_path):
            self._initialize_db()
            self._informar({}, [], [])  # un CSV borrado se nota: baja de N personas a 0
            return {}, []
        try:
            if os.path.getsize(self.csv_path) > MAX_DB_BYTES:
                raise self._error_lectura("La base de datos es demasiado grande.")
            with open(self.csv_path, mode="rb") as f:
                lines = f.read().splitlines()
        except OSError as err:
            raise self._error_lectura(f"No se puede leer la base de datos: {err}") from err
        if len(lines) > MAX_DB_ROWS:
            raise self._error_lectura("La base de datos tiene demasiadas filas.")

        records: Dict[str, str] = {}
        raros: List[bytes] = []
        duplicados: List[str] = []
        # La primera línea es la cabecera; las filas se leen por posición, sin fiarse de ella
        # La primera línea solo se salta si es de verdad la cabecera: antes se descartaba
        # siempre y un CSV sin cabecera perdía su primera persona al reescribirse
        inicio = 1 if lines and lines[0].replace(b"\xef\xbb\xbf", b"").strip().lower() == b"id,template" else 0
        for raw in lines[inicio:]:
            if not raw.strip():
                continue
            try:
                row = next(csv.reader([raw.decode("utf-8")]))
            except (UnicodeDecodeError, csv.Error, StopIteration):
                raros.append(raw)  # un byte o un campo malo solo invalida su fila
                continue
            uid, t_str = (row[0].strip(), row[1].strip()) if len(row) >= 2 else ("", "")
            try:
                # Mismas reglas que al inscribir, consultar y borrar: un id que no las cumple
                # no entra (se conserva en bruto), porque luego no se podría gestionar
                uid = parse_input_id(uid) if uid else ""
            except (TypeError, ValueError):
                uid = ""
            if uid and t_str:
                if uid in records:
                    duplicados.append(uid)  # gana la última fila (la anterior se pierde al reescribir)
                records[uid] = t_str
            else:
                raros.append(raw)
        self._informar(records, raros, duplicados)
        return records, raros

    def _error_lectura(self, mensaje: str) -> DatabaseReadError:
        """Registra el fallo de lectura (una vez por versión del fichero, no por petición) y lo devuelve para lanzarlo."""
        clave = (self._huella(), mensaje)
        if clave != self._ultimo_error:
            self._ultimo_error = clave
            logger.warning("Base de datos '%s': %s", self.csv_path, mensaje)
        return DatabaseReadError(mensaje)

    def _informar(self, records: Dict[str, str], raros: List[bytes], duplicados: List[str]) -> None:
        """
        Resumen de cada lectura real del CSV (la caché evita releerlo mientras no cambie, así que
        sale un aviso por cada cambio del fichero y no por cada petición). WARNING si hay filas
        descartadas, ids duplicados o menos personas que en la lectura anterior; si no, INFO.
        Solo se registran cifras e ids (estos pasan por limpiar(): un id con U+2028 o un número con punto sale
        modificado); nunca el contenido de las filas.
        """
        n = len(records)
        validas = n + len(duplicados)
        anterior, self._ultimo_n = self._ultimo_n, n
        clave = (self._huella(), validas, len(raros), n)
        if clave == self._ultimo_informe:
            return
        self._ultimo_informe = clave
        nivel = logging.INFO
        partes = [f"{validas} filas válidas ({n} personas), {len(raros)} descartadas"]
        if raros:
            nivel = logging.WARNING
        if duplicados:
            nivel = logging.WARNING
            unicos = sorted(set(duplicados))
            resto = f" y {len(unicos) - 5} más" if len(unicos) > 5 else ""
            partes.append("ids duplicados (gana la última fila): " + ", ".join(limpiar(i) for i in unicos[:5]) + resto)
        if anterior is not None and n < anterior:
            nivel = logging.WARNING
            partes.append(f"el número de personas ha bajado de {anterior} a {n} desde la lectura anterior "
                          "(posible truncado o borrado del CSV)")
        logger.log(nivel, "Base de datos '%s': %s", self.csv_path, "; ".join(partes))

    def _write_all_records(self, records: Dict[str, str], raros: Optional[List[bytes]] = None) -> None:
        """
        Escribe en un fichero temporal y luego sustituye el original: así una
        interrupción a mitad nunca deja la BD vacía o truncada.
        """
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["id", "template"])
        for uid, t_str in records.items():
            writer.writerow([uid, t_str])
        data = buf.getvalue().encode("utf-8") + b"".join(r + b"\r\n" for r in (raros or []))
        # Los mismos topes que aplica _leer: escribir un fichero que el propio lector rechaza dejaría la BD
        # ilegible para siempre (también para borrar, que empieza leyendo)
        lineas = 1 + len(records) + len(raros or [])
        if lineas > MAX_DB_ROWS or len(data) > MAX_DB_BYTES:
            raise DatabaseCapacityError(
                f"se superarían los límites de la base de datos ({lineas} filas y {len(data)} bytes; "
                f"máximo {MAX_DB_ROWS} filas y {MAX_DB_BYTES} bytes)")
        # Nombre único creado en exclusiva (O_EXCL): no sigue enlaces ni choca con otro escritor
        directorio = os.path.dirname(os.path.abspath(self.csv_path))
        fd, tmp_path = tempfile.mkstemp(dir=directorio, prefix=".database-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            if hasattr(os, "chmod"):
                os.chmod(tmp_path, 0o644)
            os.replace(tmp_path, self.csv_path)
        except BaseException:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise
