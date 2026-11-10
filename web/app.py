"""
Interfaz web (FastAPI) para inscribir, verificar, identificar y comparar fotos.
Verificar e identificar solo devuelven la decisión (nunca la similitud ni un ranking) y,
si hay coincidencia, en qué zonas de la cara se parecen (src/explicador.py). Comparar
(dos fotos subidas, sin tocar la BD) sí devuelve la similitud.

Seguridad propia de la web:
- Solo responde a peticiones dirigidas a localhost (DNS rebinding).
- Las peticiones que cambian algo exigen la cabecera X-CJAR: otra web abierta en el
  navegador no puede enviarla, así que no puede inscribir ni borrar a nadie (CSRF).
- Cabeceras de seguridad (CSP, nosniff, sin iframes...) y sin documentación de la API.
- Límite de tamaño antes de leer el cuerpo de la petición.
- Las fotos guardadas se vuelven a codificar, sin metadatos ni nada del fichero original.
- Inscribir, borrar, ver la lista de personas y sus fotos exige la clave de administración
  (cabecera X-CJAR-Token). Se toma de CJAR_ADMIN_TOKEN o, si no está, se genera una al
  arrancar y se muestra en el log. Verificar, identificar y comparar no la necesitan.

Uso, desde la raíz del repositorio:
    uvicorn web.app:app --port 8765 --no-server-header
Para acceder con otro nombre de host: CJAR_ALLOWED_HOSTS=localhost,127.0.0.1,mi-host
"""

import base64
import hashlib
import hmac
import os
import re
import secrets
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from src.biometric_system import BiometricSystem
from src.database import DatabaseReadError
from src.explicador import Explicador
from src.registro import avisar_si_no_escribible, configurar_para_servidor, fallo
from src.utils import average_templates, parse_input_id, parse_input_image

STATIC_DIR = Path(__file__).parent / "static"
configurar_para_servidor()  # los avisos del logger "cjar" salen en el log del contenedor, con formato
FOTOS_DIR = Path(__file__).parent.parent / "fotos"
try:
    FOTOS_DIR.mkdir(exist_ok=True)
except OSError:
    pass  # no se detiene el arranque: el aviso de la línea siguiente explica qué hacer
avisar_si_no_escribible(str(FOTOS_DIR), "guardar las fotos de inscripción")
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_FOTOS = 10
# Peticiones pesadas admitidas a la vez (una trabajando y el resto esperando el lock);
# las demás reciben 503 en vez de acumular memoria y hilos
MAX_EN_COLA = 4
MAX_BODY_BYTES = MAX_FOTOS * MAX_UPLOAD_BYTES + 1024 * 1024
# Lo que realmente necesita cada ruta anónima: una foto (verificar/identificar) o dos (comparar)
LIMITE_RUTA = {
    "/api/verificar": MAX_UPLOAD_BYTES + 1024 * 1024,
    "/api/identificar": MAX_UPLOAD_BYTES + 1024 * 1024,
    "/api/comparar": 2 * MAX_UPLOAD_BYTES + 1024 * 1024,
}
# Bytes de cuerpo en vuelo. Starlette vuelca a /tmp (tmpfs de 256 MiB en docker-compose.yml) toda parte de
# fichero de más de 1 MiB MIENTRAS recibe la petición, es decir, antes de turno(): sin un tope agregado,
# unas 25 subidas anónimas incompletas llenan /tmp y las demás (también las del administrador) reciben
# 400 "error parsing the body". Se reserva el Content-Length declarado hasta que acaba la petición, con un
# presupuesto para anónimos y otro, aparte, para el administrador; la suma (~200 MiB) queda por debajo del tmpfs.
MIB = 1024 * 1024
PRESUPUESTO_ANONIMO = 96 * MIB
PRESUPUESTO_ADMIN = MAX_BODY_BYTES + 4 * MIB
FOTO_MAX_LADO = 800  # las fotos guardadas solo se usan como miniatura y para el cotejo
ID_PATTERN = re.compile(r"\w[\w()\-. ]{0,63}")
ADMIN_TOKEN = os.environ.get("CJAR_ADMIN_TOKEN", "").strip()
if len(ADMIN_TOKEN) < 16:
    if ADMIN_TOKEN:
        print("CJAR_ADMIN_TOKEN es demasiado corta (mín. 16 caracteres): se genera otra.", file=sys.stderr)
    ADMIN_TOKEN = secrets.token_urlsafe(24)
    print(f"Clave de administración de CJAR (inscribir/borrar): {ADMIN_TOKEN}", file=sys.stderr, flush=True)
ALLOWED_HOSTS = [h.strip() for h in os.environ.get("CJAR_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]

CSP = (
    "default-src 'self'; script-src 'self'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
    "img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; "
    "object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(self), microphone=(), geolocation=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cache-Control": "no-store",
}

class PresupuestoCuerpos:
    """Cuenta los bytes de cuerpo declarados de las peticiones en curso, por separado para anónimos y administrador.
    Solo se toca desde el bucle de eventos y sin esperas entre comprobar y sumar, así que no necesita lock."""

    def __init__(self, anonimo: int, admin: int):
        self.limite = {False: anonimo, True: admin}
        self.usado = {False: 0, True: 0}

    def reservar(self, n: int, admin: bool) -> bool:
        if self.usado[admin] + n > self.limite[admin]:
            return False
        self.usado[admin] += n
        return True

    def liberar(self, n: int, admin: bool) -> None:
        self.usado[admin] = max(0, self.usado[admin] - n)


cuerpos = PresupuestoCuerpos(PRESUPUESTO_ANONIMO, PRESUPUESTO_ADMIN)

app = FastAPI(title="CJAR", docs_url=None, redoc_url=None, openapi_url=None)
system = BiometricSystem(db_path="database/database.csv")
explicador = Explicador(system.extractor)
# El CSV y los modelos no están pensados para accesos concurrentes
lock = threading.Lock()
cola = threading.BoundedSemaphore(MAX_EN_COLA)


@contextmanager
def turno():
    """Plaza en la cola de trabajo pesado, o 503 si está llena."""
    if not cola.acquire(blocking=False):
        raise HTTPException(503, "El servidor está ocupado. Inténtalo de nuevo en unos segundos.")
    try:
        yield
    finally:
        cola.release()


@app.middleware("http")
async def seguridad(request: Request, call_next):
    reservado, es_admin = 0, False
    if request.method not in ("GET", "HEAD"):
        # Un formulario o fetch "simple" desde otra web no puede añadir cabeceras propias
        if request.headers.get("x-cjar") != "1":
            return JSONResponse({"detail": "Petición rechazada."}, status_code=403, headers=SECURITY_HEADERS)
        # Las rutas de administración se rechazan sin clave ANTES de leer el cuerpo: así un
        # anónimo no hace que se analice un multipart de decenas de MB para recibir un 401
        if _es_ruta_admin(request) and not _es_admin(request):
            return JSONResponse({"detail": "Hace falta la clave de administración."}, status_code=401, headers=SECURITY_HEADERS)
        # Solo se admite Content-Length: con Transfer-Encoding (o con las dos cabeceras a la vez)
        # el servidor delimita el cuerpo de otra forma y el tope por cabecera no valdría
        if "transfer-encoding" in request.headers:
            return JSONResponse({"detail": "No se admite Transfer-Encoding."}, status_code=400, headers=SECURITY_HEADERS)
        length = request.headers.get("content-length")
        if length is None and request.method == "POST":
            return JSONResponse({"detail": "Falta el tamaño de la petición."}, status_code=411, headers=SECURITY_HEADERS)
        if length is not None and (not length.isdigit() or int(length) > LIMITE_RUTA.get(request.url.path, MAX_BODY_BYTES)):
            return JSONResponse({"detail": "La petición es demasiado grande."}, status_code=413, headers=SECURITY_HEADERS)
        if length is not None and int(length) > 0:
            es_admin = _es_admin(request)
            if not cuerpos.reservar(int(length), es_admin):
                return JSONResponse({"detail": "El servidor está recibiendo otras subidas. Inténtalo de nuevo en unos segundos."},
                                    status_code=503, headers={**SECURITY_HEADERS, "Retry-After": "5"})
            reservado = int(length)
    try:
        response = await call_next(request)
    finally:
        if reservado:
            cuerpos.liberar(reservado, es_admin)
    response.headers.update(SECURITY_HEADERS)
    return response


app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)


@app.exception_handler(DatabaseReadError)
async def bd_ilegible(request: Request, exc: DatabaseReadError):
    # Fallo cerrado: sin BD legible no se verifica, identifica ni inscribe a nadie
    return JSONResponse({"detail": "No se puede leer la base de datos."}, status_code=503, headers=SECURITY_HEADERS)


def _to_data_url(img: np.ndarray) -> str:
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        return ""
    return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode()


def _read_upload(foto: UploadFile) -> bytes:
    # Se lee como mucho un byte más del límite: nunca se carga entero un fichero enorme
    data = foto.file.read(MAX_UPLOAD_BYTES + 1)
    if not data:
        raise HTTPException(422, "No se ha recibido ninguna imagen.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "La imagen es demasiado grande (máx. 10 MB).")
    return data


MODOS = ("completo", "sd")


def _modo(modo: str, decision: bool = False) -> str:
    """
    Valida el flujo pedido. En verificar/identificar solo se admite el completo: sin detector
    la imagen entra tal cual al extractor y se podría fabricar una imagen sintética que
    reproduzca la plantilla (pública) de alguien.
    """
    if modo not in MODOS:
        raise HTTPException(422, "Flujo no válido.")
    if decision and modo != "completo":
        raise HTTPException(422, "Verificar e identificar solo admiten el flujo completo (con detector).")
    return modo


def _cara(data: bytes, modo: str) -> Tuple[np.ndarray, np.ndarray]:
    """Obtiene el template y la imagen 112x112 que entra al extractor."""
    try:
        if modo == "sd":
            face = cv2.resize(parse_input_image(data), (112, 112), interpolation=cv2.INTER_AREA)
        else:
            face = system.detector.detect_and_align(data)
        return system.extractor.extract(face), face
    except ValueError as err:
        raise HTTPException(422, str(err))
    except Exception as exc:
        # Una imagen inválida es ValueError (esperable, no se registra); esto es otra cosa
        fallo("web._cara", exc, limitar=True)
        raise HTTPException(422, "No se ha podido procesar la imagen.")


def _template(data: bytes, modo: str) -> Tuple[np.ndarray, str]:
    """Como _cara, pero con la cara ya codificada para mostrarla en la web."""
    t, face = _cara(data, modo)
    return t, _to_data_url(face)


def _foto_path(id: str) -> Path:
    """
    Ruta de la foto de un id. El nombre del fichero es el SHA-256 del id exacto, no el id:
    así cada id de la BD tiene exactamente un fichero y ids distintos nunca comparten foto
    ('./v', 'x/../v' o 'V' en un montaje sin distinción de mayúsculas llegaban a la de 'v').
    Además no puede contener '/', '..' ni salir de FOTOS_DIR.
    """
    return FOTOS_DIR / (hashlib.sha256(id.encode("utf-8")).hexdigest() + ".jpg")


def _guardar_foto(id: str, data: bytes) -> None:
    """
    Guarda la foto de inscripción reconstruida a partir de sus píxeles (JPEG nuevo y
    reducido): no se escribe en disco ni un byte del fichero que ha mandado el usuario.
    """
    p = _foto_path(id)
    img = parse_input_image(data)
    escala = min(1.0, FOTO_MAX_LADO / max(img.shape[:2]))
    if escala < 1.0:
        img = cv2.resize(img, None, fx=escala, fy=escala, interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if ok:
        p.write_bytes(buf.tobytes())


def _guardar_foto_o_avisar(id: str, data: bytes, existia: bool) -> bool:
    """
    Guarda la foto de inscripción; devuelve True si se guardó. La foto es un dato derivado y
    opcional (el cotejo y las explicaciones funcionan sin ella), y la plantilla ya está inscrita:
    si no se puede escribir (fotos/ sin permiso, disco lleno...) se deja un WARNING y la inscripción
    sigue, en vez de responder 500 con la plantilla guardada. Si se estaba sustituyendo a la persona
    se intenta borrar la foto anterior, para no enseñar la cara de la versión vieja (si fotos/ no
    es escribible tampoco se podrá borrar y la anterior se queda).
    """
    try:
        _guardar_foto(id, data)
        return True
    except OSError as exc:
        fallo("web._guardar_foto", exc)
        if existia:
            try:
                _foto_path(id).unlink(missing_ok=True)
            except OSError:
                pass
        return False


def _cara_inscrita(id: str, modo: str) -> Optional[np.ndarray]:
    """Cara de la foto guardada al inscribir a 'id' (si la hay), para explicar por los dos lados."""
    p = _foto_path(id)
    if not p.is_file() or p.is_symlink():
        return None
    try:
        return _cara(p.read_bytes(), modo)[1]
    except HTTPException:
        return None


def _explicacion(cara_a: np.ndarray, t_b: np.ndarray,
                 cara_b: Optional[np.ndarray] = None, t_a: Optional[np.ndarray] = None) -> Optional[dict]:
    """Zonas de la cara en las que se apoya la similitud (ver src/explicador.py)."""
    try:
        r = explicador.explicar(cara_a, t_b, cara_b, t_a)
    except Exception as exc:
        fallo("web._explicacion", exc, limitar=True)
        return None  # la explicación es un extra: si falla, el resultado se muestra igual
    return {
        "regiones": r["regiones"],
        "mapas": [_to_data_url(m) if m is not None else None for m in r["mapas"]],
    }


def _sin_foto_inscrita(explicacion: Optional[dict], request: Request) -> Optional[dict]:
    """
    El mapa de la segunda cara es la foto guardada de la persona inscrita, que solo ve el
    administrador (/fotos): a quien no lo es se le quita, también si acaba de coincidir.
    """
    if explicacion and not _es_admin(request):
        explicacion = {**explicacion, "mapas": [explicacion["mapas"][0], None]}
    return explicacion


def _clean_id(id: str, nuevo: bool = False) -> str:
    """
    Valida el identificador. Para inscribir (nuevo=True) solo se admiten letras, números,
    espacios y _ - . ( ), empezando por letra o número; para consultar basta con que sea
    un id válido para la BD.
    """
    try:
        clean = parse_input_id(id)
    except (TypeError, ValueError) as err:
        raise HTTPException(422, f"Identificador no válido: {err}")
    if nuevo and not ID_PATTERN.fullmatch(clean):
        raise HTTPException(422, "Identificador no válido: usa letras, números, espacios y _ - . ( ) (máx. 64 caracteres).")
    return clean


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/app.js")
def app_js():
    return FileResponse(STATIC_DIR / "app.js", media_type="text/javascript")


def _es_ruta_admin(request: Request) -> bool:
    ruta = request.url.path
    return ruta == "/api/inscribir" or ruta.startswith("/api/personas/") or ruta.startswith("/fotos/")


def _es_admin(request: Request) -> bool:
    token = request.headers.get("x-cjar-token", "")
    return hmac.compare_digest(token.encode("utf-8", "replace"), ADMIN_TOKEN.encode())


def admin(request: Request) -> None:
    """Dependencia de las operaciones de administración: exige la clave."""
    if not _es_admin(request):
        raise HTTPException(401, "Hace falta la clave de administración.")


@app.get("/api/info")
def info(request: Request):
    """Sin clave solo se da el número de inscritos: la lista de ids es de administración."""
    # Sin el lock global: leer el CSV es seguro (se sustituye de forma atómica) y así esta
    # consulta ligera no espera a que acabe una inscripción o una verificación
    personas = sorted(system.db.get_all().keys())
    datos = {"umbral": system.matcher.threshold, "inscritos": len(personas), "admin": _es_admin(request)}
    if datos["admin"]:
        datos["personas"] = personas
    return datos


@app.delete("/api/personas/{id}", dependencies=[Depends(admin)])
def borrar(id: str):
    # El mismo id normalizado para la plantilla y para la foto (antes " x" borraba la
    # plantilla de "x" pero dejaba su foto)
    clean_id = _clean_id(id)
    with lock:
        if not system.db.delete_template(clean_id):
            raise HTTPException(404, "Esa persona no está inscrita.")
    foto_path = _foto_path(clean_id)
    if foto_path.is_file():
        foto_path.unlink()
    return {"ok": True}


@app.get("/fotos/{filename}", dependencies=[Depends(admin)])
def get_foto(filename: str):
    """La foto se pide como /fotos/<id>.jpg; el fichero real se busca por el hash del id."""
    if filename.endswith(".jpg"):
        try:
            p = _foto_path(parse_input_id(filename[:-4]))
        except (TypeError, ValueError):
            p = None
        if p and p.is_file() and not p.is_symlink():
            return FileResponse(p, media_type="image/jpeg")
    raise HTTPException(404, "Foto no encontrada")


@app.post("/api/inscribir", dependencies=[Depends(admin)])
def inscribir(id: str = Form(...), fotos: List[UploadFile] = File(...), modo: str = Form("completo"),
                    reemplazar: bool = Form(False)):
    """
    Inscribe con una o varias fotos: el template guardado es la media de todas. Si el id
    ya existe responde 409, salvo que se pida reemplazar=true.
    """
    clean_id = _clean_id(id, nuevo=True)
    modo = _modo(modo)
    if len(fotos) > MAX_FOTOS:
        raise HTTPException(422, f"Como máximo {MAX_FOTOS} fotos por inscripción.")
    with turno():
        datos = [_read_upload(f) for f in fotos]  # en memoria solo dentro de la cola
        with lock:
            return _inscribir(clean_id, datos, modo, reemplazar)


def _inscribir(clean_id: str, datos: List[bytes], modo: str, reemplazar: bool) -> dict:
    templates, caras = [], []
    for i, data in enumerate(datos, 1):
        try:
            t, c = _template(data, modo)
        except HTTPException as err:
            raise HTTPException(err.status_code, f"Foto {i}: {err.detail}")
        templates.append(t)
        caras.append(c)
    media = average_templates(templates)
    existia = system.db.get_template(clean_id) is not None
    if not system.db.save_template(clean_id, media, reemplazar=reemplazar):
        if system.db.existe(clean_id) and not reemplazar:
            raise HTTPException(409, f"«{clean_id}» ya está inscrito. Confirma si quieres sustituirlo.")
        # save_template también devuelve False si la BD llegaría a sus topes de filas o de tamaño (queda un WARNING en el log)
        raise HTTPException(507, "La base de datos ha alcanzado su tamaño máximo: no se ha inscrito a nadie.")
    foto_guardada = _guardar_foto_o_avisar(clean_id, datos[0], existia)
    # Cada foto contra la media de las demás (se lee como una verificación):
    # si no llega al umbral suele ser una foto mala o de otra persona
    coherencia = [
        system.matcher.compute_similarity(t, average_templates(templates[:i] + templates[i + 1:]))
        for i, t in enumerate(templates)
    ] if len(templates) > 1 else []
    return {
        "ok": True, "id": clean_id, "actualizado": existia, "foto_guardada": foto_guardada, "caras": caras,
        "coherencia": coherencia, "umbral": system.matcher.threshold,
    }


@app.post("/api/verificar")
def verificar(
    request: Request,
    id: str = Form(...),
    foto: UploadFile = File(...),
    modo: str = Form("completo"),
):
    """
    Solo devuelve la decisión. La similitud no sale de aquí: con ella se podría ir
    retocando una imagen hasta parecerse a la persona (hill-climbing). La respuesta
    (y el trabajo hecho) es la misma si el id no existe, para no delatar quién está inscrito.
    """
    clean_id = _clean_id(id)
    modo = _modo(modo, decision=True)
    with turno(), lock:
        data = _read_upload(foto)
        template, face = _cara(data, modo)
        enrolled = system.db.get_template(clean_id)
        match = enrolled is not None and system.matcher.verify_one_to_one(template, enrolled)[0]
        # La explicación compara con la plantilla guardada: solo se da si ya hay coincidencia
        explicacion = _explicacion(face, enrolled, _cara_inscrita(clean_id, modo), template) if match else None
    return {"match": match, "umbral": system.matcher.threshold, "cara": _to_data_url(face),
            "explicacion": _sin_foto_inscrita(explicacion, request)}


@app.post("/api/identificar")
def identificar(
    request: Request,
    foto: UploadFile = File(...),
    modo: str = Form("completo"),
):
    """Solo devuelve el id si alguien supera el umbral; ni ranking ni similitudes."""
    modo = _modo(modo, decision=True)
    with turno(), lock:
        data = _read_upload(foto)
        template, face = _cara(data, modo)
        todos = system.db.get_all()
        mejor, _ = system.matcher.identify_one_to_many(template, todos)
        explicacion = _explicacion(face, todos[mejor], _cara_inscrita(mejor, modo), template) if mejor else None
    return {"id": mejor, "umbral": system.matcher.threshold, "cara": _to_data_url(face),
            "explicacion": _sin_foto_inscrita(explicacion, request)}


@app.post("/api/comparar")
def comparar(
    foto1: UploadFile = File(...),
    foto2: UploadFile = File(...),
    modo: str = Form("completo"),
):
    modo = _modo(modo)
    th = system.matcher.threshold  # el umbral solo lo decide el servidor
    with turno(), lock:
        d1, d2 = _read_upload(foto1), _read_upload(foto2)
        t1, f1 = _cara(d1, modo)
        t2, f2 = _cara(d2, modo)
        score = system.matcher.compute_similarity(t1, t2)
        explicacion = _explicacion(f1, t2, f2, t1)
    return {
        "match": score >= th, "score": score, "umbral": th,
        "caras": [_to_data_url(f1), _to_data_url(f2)], "explicacion": explicacion,
    }


# Cualquier otra ruta que no sea de la API (ej. /index.html) muestra la página
@app.get("/{ruta:path}", include_in_schema=False)
def spa(ruta: str):
    if ruta.startswith("api/"):
        raise HTTPException(404, "Endpoint no encontrado.")
    return FileResponse(STATIC_DIR / "index.html")
