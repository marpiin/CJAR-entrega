"""
Registro de fallos con logging.getLogger("cjar").

BiometricSystem sigue sin lanzar excepciones (devuelve False/None), pero cada fallo deja un
WARNING con el método, el tipo de excepción y el mensaje de la excepción tratado por limpiar().

Qué hace limpiar() con el mensaje:
  1. lo recorta a LIMITE_ENTRADA (500) caracteres, antes de cualquier expresión regular;
  2. lo deja en una sola línea (colapsa saltos de línea y espacios, también U+2028/U+2029 y NEL);
  3. sustituye por '?' los caracteres de control (ESC, NUL, retroceso, DEL...);
  4. sustituye por '…' lo que va entre comillas simples o dobles (si el recorte deja unas comillas
     sin cerrar, también lo que quede detrás);
  5. sustituye por <num> los números decimales, los de notación científica y nan/inf;
  6. recorta el resultado a MAX_MENSAJE (200) caracteres.

Qué NO se garantiza: que el log no contenga datos. Pasan tal cual los números enteros y
hexadecimales, cualquier texto que no vaya entre comillas (un token, una clave), las rutas de
ficheros (p. ej. «No existe el archivo de imagen en la ruta: /home/ana/pasaporte.jpg»), el
nombre del método y el tipo de excepción. Unas comillas mezcladas dentro de un mismo texto
(ab'cd"ef) pueden dejar pasar un fragmento. La clave de administración no llega a este módulo
por ningún camino que se conozca, pero limpiar() no la censura: si un mensaje la contuviera sin
comillas, saldría en el log. Las fotos y las plantillas no se registran como tales: solo el
mensaje de la excepción, que puede mencionar un fragmento de ellas.
"""

import logging
import os
import re
import sys
import tempfile
import time
import unicodedata

logger = logging.getLogger("cjar")

_ENTRE_COMILLAS = re.compile(r"'[^']*'|\"[^\"]*\"")
# Igual, pero una comilla sin cierre al final se lleva el resto: se usa solo cuando se ha recortado el
# mensaje, para que el recorte no deje a medias un texto entre comillas (y se vea su principio)
_ENTRE_COMILLAS_CORTADO = re.compile(r"'[^']*(?:'|\Z)|\"[^\"]*(?:\"|\Z)")
# decimales (0.123, .5), notación científica (1e30) y los valores nan/inf que imprimen numpy y float()
_NUMERO = re.compile(r"[-+]?\d*\.\d+(?:[eE][-+]?\d+)?|[-+]?\d+[eE][-+]?\d+|\b(?:nan|inf)\b", re.IGNORECASE)
LIMITE_ENTRADA = 500   # se recorta el mensaje original a esto ANTES de aplicar las expresiones regulares
MAX_MENSAJE = 200      # y el mensaje ya limpio a esto

_ultimo_aviso: dict = {}
SEGUNDOS_ENTRE_AVISOS_IGUALES = 5.0


def limpiar(texto) -> str:
    """
    Mensaje de una excepción para el log: una sola línea, con los caracteres de control sustituidos por
    '?', sin lo que va entre comillas ni los números decimales. Se recorta a LIMITE_ENTRADA caracteres antes de las expresiones regulares:
    la de los números tiene coste cuadrático con rachas largas de dígitos (30.000 dígitos tardaban 5 s).
    """
    bruto = str(texto)
    cortado = len(bruto) > LIMITE_ENTRADA
    t = " ".join(bruto[:LIMITE_ENTRADA].split())
    # ESC, NUL, retroceso, DEL, CSI de C1, marcas bidireccionales, espacios de ancho cero... (categorías Unicode C*):
    # los saltos de línea y espacios ya se han colapsado arriba; aquí un ESC ya no puede colorear ni borrar una terminal
    t = "".join("?" if unicodedata.category(c).startswith("C") else c for c in t)
    t = (_ENTRE_COMILLAS_CORTADO if cortado else _ENTRE_COMILLAS).sub("'…'", t)
    t = _NUMERO.sub("<num>", t)
    return t[:MAX_MENSAJE]

def fallo(metodo: str, exc: BaseException, limitar: bool = False) -> None:
    """
    WARNING «<método> falló: <TipoDeExcepción>: <mensaje limpio>». Con limitar=True (fallos que
    puede provocar un cliente web) solo se registra 1 aviso por (método, tipo de excepción) cada
    SEGUNDOS_ENTRE_AVISOS_IGUALES, aunque el texto del mensaje cambie (p. ej. un tamaño de imagen
    distinto en cada petición): limitar por el texto completo se saltaba con solo variarlo.

    NUNCA lanza: se llama dentro de un `except` de BiometricSystem, y un fallo al registrar otro
    fallo (p. ej. una excepción cuyo __str__ lanza) rompería el contrato de que esos métodos solo
    devuelven False/None. Si no se puede obtener el mensaje se registra solo el tipo.
    """
    try:
        try:
            mensaje = limpiar(exc)
        except Exception:
            mensaje = "(mensaje no disponible)"
        nombre = type(exc).__name__
        texto = f"{metodo} falló: {nombre}: {mensaje}"
        if limitar:
            clave = (metodo, nombre)
            ahora = time.monotonic()
            if ahora - _ultimo_aviso.get(clave, -1e9) < SEGUNDOS_ENTRE_AVISOS_IGUALES:
                return
            _ultimo_aviso[clave] = ahora
            if len(_ultimo_aviso) > 256:
                _ultimo_aviso.clear()
        logger.warning("%s", texto)
    except Exception:
        pass  # registrar un fallo no puede provocar otro


def configurar_para_servidor() -> None:
    """
    uvicorn solo configura sus propios loggers: sin esto los avisos de "cjar" saldrían sin
    formato (handler de último recurso) o no saldrían. Los envía a stderr, que es lo que
    muestra `docker compose logs`. No hace nada si ya hay un handler.
    """
    if logger.handlers:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


_directorios_avisados: set = set()


def avisar_si_no_escribible(directorio: str, para: str) -> bool:
    """
    Comprueba con una escritura real (un fichero temporal que se borra al instante) que el usuario
    actual puede escribir en 'directorio' y, si no, deja un WARNING con la solución. No lanza
    excepciones ni detiene nada. Devuelve True si se puede escribir. Avisa una sola vez por directorio.
    """
    try:
        with tempfile.TemporaryFile(dir=directorio):
            return True
    except OSError as err:
        ruta = os.path.abspath(directorio)
        if ruta in _directorios_avisados:
            return False
        _directorios_avisados.add(ruta)
        uid = f" (uid={os.getuid()}, gid={os.getgid()})" if hasattr(os, "getuid") else ""
        motivo = "no existe" if isinstance(err, FileNotFoundError) else "no es escribible por el usuario actual"
        logger.warning(
            "El directorio '%s' %s%s: no se podrá %s. Solución: en la carpeta del proyecto ejecuta "
            "'chown 1000:1000 database fotos', o arranca el contenedor con el usuario propietario de esas "
            "carpetas: CJAR_UID=$(id -u) CJAR_GID=$(id -g) docker compose up --build (ver el README).",
            ruta, motivo, uid, para)
        return False
