"""
Protección de las plantillas: rotación secreta, cifrado de cada fila y sello del fichero entero.

- Plantilla cancelable: el extractor multiplica la salida de ArcFace por una matriz ortogonal secreta de
  512x512 derivada de la clave. El coseno entre dos plantillas del sistema no cambia, pero una plantilla
  calculada con otro ArcFace, o filtrada de otra instalación o de antes de cambiar la clave, no sirve aquí.
- Cada plantilla se cifra con AES-256-GCM (nonce aleatorio de 96 bits por escritura) y el id de la
  persona va como dato autenticado: una fila robada no sirve como plantilla, y una fila movida a otro
  id, modificada o inventada no descifra.
- La última línea del CSV es un sello HMAC-SHA256 de todo lo anterior, con un contador de versión:
  detecta filas añadidas, borradas, duplicadas o reordenadas y el truncado del fichero.

La clave (32 bytes) está FUERA del CSV: CJAR_CLAVE_BD (64 caracteres hexadecimales) o el fichero
CJAR_CLAVE_BD_FICHERO (por defecto clave/bd.key). Con HKDF se sacan tres subclaves independientes:
para cifrar, para el sello y para la rotación.

Qué NO protege: quien tenga la clave (o ejecute código dentro del proceso) puede leer y escribir la
BD, y restaurar una copia antigua ENTERA y legítima del CSV con el sistema parado no se distingue de
la original (en la misma ejecución sí se detecta, por el contador).
"""

import base64
import binascii
import hashlib
import hmac
import os
import secrets
from functools import lru_cache
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

PREFIJO = "cjar1:"
ID_SELLO = "__sello__"
_DIM = 512
_NONCE = 12
FICHERO_CLAVE = Path(__file__).resolve().parent.parent / "clave" / "bd.key"


class ClaveError(RuntimeError):
    """Falta la clave de la BD o no es válida."""


class IntegridadError(ValueError):
    """Una fila o el sello no se corresponden con la clave: alguien ha tocado el fichero."""


def _clave_desde_texto(texto: str, origen: str) -> bytes:
    texto = texto.strip()
    try:
        clave = bytes.fromhex(texto)
    except ValueError:
        clave = b""
    if len(clave) != 32:
        raise ClaveError(f"La clave de la base de datos ({origen}) debe tener 64 caracteres hexadecimales.")
    return clave


def cargar_clave() -> bytes:
    """Clave de la BD desde el entorno o desde el fichero. Lanza ClaveError si no hay o no es válida."""
    texto = os.environ.get("CJAR_CLAVE_BD", "").strip()
    if texto:
        return _clave_desde_texto(texto, "CJAR_CLAVE_BD")
    ruta = Path(os.environ.get("CJAR_CLAVE_BD_FICHERO", "").strip() or FICHERO_CLAVE)
    try:
        return _clave_desde_texto(ruta.read_text(encoding="ascii"), str(ruta))
    except (OSError, UnicodeDecodeError) as err:
        raise ClaveError(
            f"No se puede leer la clave de la base de datos en '{ruta}'. Es el fichero clave/bd.key del "
            "repositorio (o define CJAR_CLAVE_BD / CJAR_CLAVE_BD_FICHERO)."
        ) from err


def nueva_clave() -> str:
    return secrets.token_hex(32)


@lru_cache(maxsize=8)
def _matriz_ortogonal(semilla: bytes) -> np.ndarray:
    """
    Matriz ortogonal de 512x512 que depende solo de la semilla: normales de Box-Muller sobre SHAKE-256 y
    descomposición QR con el signo fijado (distribución uniforme). No usa np.random para no depender de
    que su generador no cambie entre versiones.
    """
    n = _DIM * _DIM
    u = np.frombuffer(hashlib.shake_256(semilla).digest(n * 16), dtype="<u8").astype(np.float64)
    u1, u2 = (u[:n] + 1.0) / 2.0**64, u[n:] / 2.0**64
    g = (np.sqrt(-2.0 * np.log(u1)) * np.cos(2.0 * np.pi * u2)).reshape(_DIM, _DIM)
    q, r = np.linalg.qr(g)
    return q * np.where(np.diag(r) < 0, -1.0, 1.0)


class Protector:
    """Cifra y descifra plantillas, calcula el sello del fichero y guarda la rotación secreta."""

    def __init__(self, clave: Optional[bytes] = None):
        clave = cargar_clave() if clave is None else bytes(clave)
        if len(clave) != 32:
            raise ClaveError("La clave de la base de datos debe tener 32 bytes.")
        self._aead = AESGCM(self._subclave(clave, b"cjar-bd-v1 cifrado"))
        self._k_sello = self._subclave(clave, b"cjar-bd-v1 sello")
        self.rotacion = _matriz_ortogonal(self._subclave(clave, b"cjar-bd-v1 rotacion"))

    @staticmethod
    def _subclave(clave: bytes, uso: bytes) -> bytes:
        return HKDF(algorithm=SHA256(), length=32, salt=None, info=uso).derive(clave)

    def proteger(self, v: np.ndarray) -> np.ndarray:
        """Vector(es) de ArcFace (512 o N x 512) -> espacio de plantillas de este sistema."""
        return (np.asarray(v, dtype=np.float64) @ self.rotacion).astype(np.float32)

    def desproteger(self, v: np.ndarray) -> np.ndarray:
        """La inversa de proteger() (la matriz es ortogonal)."""
        return (np.asarray(v, dtype=np.float64) @ self.rotacion.T).astype(np.float32)

    @staticmethod
    def _contexto(uid: str) -> bytes:
        return b"cjar-bd-v1|plantilla|" + uid.encode("utf-8")

    def cifrar(self, uid: str, template: np.ndarray) -> str:
        datos = np.asarray(template, dtype="<f4")
        if datos.shape != (_DIM,):
            raise ValueError("Plantilla con una dimensión inesperada.")
        nonce = secrets.token_bytes(_NONCE)
        cifrado = self._aead.encrypt(nonce, datos.tobytes(), self._contexto(uid))
        return PREFIJO + base64.b64encode(nonce + cifrado).decode("ascii")

    def descifrar(self, uid: str, texto: str) -> np.ndarray:
        """Plantilla de 'uid'. Lanza IntegridadError si la fila no es auténtica o es de otro id."""
        if not texto.startswith(PREFIJO):
            raise IntegridadError("La fila no está cifrada.")
        try:
            datos = base64.b64decode(texto[len(PREFIJO):], validate=True)
            claro = self._aead.decrypt(datos[:_NONCE], datos[_NONCE:], self._contexto(uid))
        except (binascii.Error, ValueError, InvalidTag) as err:
            raise IntegridadError("La fila no es auténtica.") from err
        if len(claro) != _DIM * 4:
            raise IntegridadError("La fila no es auténtica.")
        return np.frombuffer(claro, dtype="<f4").astype(np.float32)

    # El sello se calcula por líneas (separadas por \n) y no por bytes: git o un editor pueden cambiar
    # los finales de línea (CRLF/LF) sin que eso altere el contenido

    def _mac(self, contador: int, lineas: List[bytes]) -> bytes:
        h = hmac.new(self._k_sello, b"cjar-bd-v1|sello|%d|%d\n" % (contador, len(lineas)), hashlib.sha256)
        for linea in lineas:
            h.update(linea)
            h.update(b"\n")
        return h.digest()

    def sellar(self, lineas: List[bytes], contador: int) -> bytes:
        """Línea del sello para 'lineas' (cabecera y filas, sin finales de línea)."""
        mac = base64.b64encode(self._mac(contador, lineas)).decode("ascii")
        return f"{ID_SELLO},{PREFIJO}{contador}:{mac}".encode("ascii")

    def comprobar_sello(self, lineas: List[bytes]) -> Tuple[List[bytes], int]:
        """(líneas sin el sello, contador). Lanza IntegridadError si falta o no coincide."""
        prefijo = f"{ID_SELLO},{PREFIJO}".encode("ascii")
        if not lineas or not lineas[-1].startswith(prefijo):
            raise IntegridadError("La base de datos no tiene sello: la ha escrito otro programa o está truncada")
        try:
            contador_txt, mac_txt = lineas[-1][len(prefijo):].split(b":")
            if not contador_txt.isdigit() or len(contador_txt) > 18:
                raise ValueError("contador")
            contador, mac = int(contador_txt), base64.b64decode(mac_txt, validate=True)
        except (ValueError, binascii.Error) as err:
            raise IntegridadError("El sello de la base de datos no es válido.") from err
        if not hmac.compare_digest(mac, self._mac(contador, lineas[:-1])):
            raise IntegridadError("El sello no coincide: el fichero se ha modificado fuera del sistema "
                                  "o la clave es otra")
        return lineas[:-1], contador
