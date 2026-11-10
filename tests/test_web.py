"""
Tests de seguridad de la interfaz web (web/app.py): CSRF, DNS rebinding, cabeceras,
ids maliciosos y tamaño de las peticiones. Usan una BD temporal, no la oficial.
Necesitan httpx2 (requirements-dev.txt).
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient

CLAVE = "clave-de-prueba-0123456789"
ADMIN = {"X-CJAR": "1", "X-CJAR-Token": CLAVE}


class TestWebSecurity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import web.app as web_app
        from src.database import BiometricDatabase

        cls.tmp = tempfile.mkdtemp()
        cls.web_app = web_app
        cls.db_original = web_app.system.db
        # Las fotos de los tests van a un directorio temporal: antes escribían (y borraban) fotos/<sha256(id)>.jpg
        # en la carpeta fotos/ REAL para ids fijos como «borrable» o «victima_foto»
        cls.fotos_original = web_app.FOTOS_DIR
        web_app.FOTOS_DIR = Path(cls.tmp) / "fotos"
        web_app.FOTOS_DIR.mkdir()
        web_app.ADMIN_TOKEN = CLAVE
        web_app.system.db = BiometricDatabase(str(Path(cls.tmp) / "database.csv"))
        t = np.random.default_rng(1).standard_normal(512)
        web_app.system.db.save_template("persona", t / np.linalg.norm(t))
        cls.client = TestClient(web_app.app, base_url="http://localhost")

    @classmethod
    def tearDownClass(cls):
        cls.web_app.system.db = cls.db_original
        cls.web_app.FOTOS_DIR = cls.fotos_original
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_csrf_sin_cabecera(self):
        r = self.client.delete("/api/personas/persona")
        self.assertEqual(r.status_code, 403)
        r = self.client.post("/api/identificar", files={"foto": ("a.jpg", b"x")})
        self.assertEqual(r.status_code, 403)
        self.assertIsNotNone(self.web_app.system.db.get_template("persona"))

    def test_host_ajeno(self):
        evil = TestClient(self.web_app.app, base_url="http://atacante.example")
        self.assertEqual(evil.get("/api/info").status_code, 400)

    def test_cabeceras_y_superficie(self):
        r = self.client.get("/")
        self.assertIn("script-src 'self'", r.headers["content-security-policy"])
        self.assertEqual(r.headers["x-content-type-options"], "nosniff")
        self.assertEqual(r.headers["x-frame-options"], "DENY")
        self.assertNotIn("openapi", self.client.get("/openapi.json").text)
        self.assertNotIn("modelo", self.client.get("/api/info").json())
        for ruta in ["/fotos/..%2Fdatabase%2Fdatabase.csv", "/fotos/../database/database.csv",
                     "/fotos/..%5Cdatabase%5Cdatabase.csv"]:
            self.assertNotIn("id,template", self.client.get(ruta).text, ruta)
        self.assertEqual(self.client.get("/fotos/notas.txt", headers=ADMIN).status_code, 404)

    def test_id_malicioso_al_inscribir(self):
        for mal in ["<script>alert(1)</script>", "../x", "=cmd", "a/b"]:
            r = self.client.post("/api/inscribir", data={"id": mal}, files={"fotos": ("a.jpg", b"x")},
                                 headers=ADMIN)
            self.assertEqual(r.status_code, 422, mal)

    def test_peticion_demasiado_grande(self):
        r = self.client.post("/api/identificar", headers={"X-CJAR": "1", "Content-Length": str(10**12)},
                             content=b"")
        self.assertEqual(r.status_code, 413)

    def test_cliente_no_interpola_src_sin_validar(self):
        # Antes: <img src="${src}"> sin escapar; ahora todo src pasa por imgSrc()
        import re
        js = (Path(__file__).resolve().parent.parent / "web" / "static" / "app.js").read_text(encoding="utf-8")
        for m in re.finditer(r'src="\$\{([^}]*)\}"', js):
            self.assertTrue(m.group(1).startswith("imgSrc("), m.group(0))
        self.assertNotIn("score", re.sub(r"cmp-go[\s\S]*", "", js.split("$('#ver-go')")[1]))

    def test_administracion_exige_clave(self):
        # Antes: cualquiera con la cabecera X-CJAR podía inscribir, borrar, listar y ver fotos
        H = {"X-CJAR": "1"}
        mala = {"X-CJAR": "1", "X-CJAR-Token": "no-es-la-clave-0000000"}
        for cab in (H, mala):
            r = self.client.post("/api/inscribir", data={"id": "intruso"}, files={"fotos": ("a.jpg", b"x")}, headers=cab)
            self.assertEqual(r.status_code, 401)
            self.assertEqual(self.client.delete("/api/personas/persona", headers=cab).status_code, 401)
            self.assertEqual(self.client.get("/fotos/persona.jpg", headers=cab).status_code, 401)
            info = self.client.get("/api/info", headers=cab).json()
            self.assertNotIn("personas", info)
            self.assertFalse(info["admin"])
        self.assertIsNotNone(self.web_app.system.db.get_template("persona"))
        info = self.client.get("/api/info", headers=ADMIN).json()
        self.assertIn("persona", info["personas"])
        self.assertEqual(info["inscritos"], len(info["personas"]))


def _unit(seed: int) -> np.ndarray:
    t = np.random.default_rng(seed).standard_normal(512)
    return (t / np.linalg.norm(t)).astype(np.float32)


class TestWebBiometria(unittest.TestCase):
    """
    Ataques a la decisión biométrica por la web. El detector y el extractor se sustituyen
    por un template controlado (_cara), así no hacen falta fotos reales.
    """

    @classmethod
    def setUpClass(cls):
        import web.app as web_app
        from src.database import BiometricDatabase

        cls.web_app = web_app
        cls.tmp = tempfile.mkdtemp()
        cls.db_original = web_app.system.db
        cls.cara_original = web_app._cara
        cls.fotos_original = web_app.FOTOS_DIR  # fotos de los tests a un directorio temporal, no a fotos/ real
        web_app.FOTOS_DIR = Path(cls.tmp) / "fotos"
        web_app.FOTOS_DIR.mkdir()
        web_app.system.db = BiometricDatabase(str(Path(cls.tmp) / "database.csv"))
        cls.victima = _unit(10)
        web_app.system.db.save_template("victima", cls.victima)
        cls.consulta = _unit(99)  # otra persona: similitud ~0 con la víctima
        web_app._cara = lambda data, modo: (cls.consulta, np.zeros((112, 112, 3), np.uint8))
        cls.client = TestClient(web_app.app, base_url="http://localhost")
        web_app.ADMIN_TOKEN = CLAVE
        cls.H = {"X-CJAR": "1"}

    @classmethod
    def tearDownClass(cls):
        cls.web_app.system.db = cls.db_original
        cls.web_app.FOTOS_DIR = cls.fotos_original
        cls.web_app._cara = cls.cara_original
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def post(self, ruta, data=None, n_fotos=1, campo="foto", headers=None):
        files = [(campo if n_fotos == 1 or campo != "fotos" else "fotos", (f"{i}.jpg", b"x", "image/jpeg"))
                 for i in range(n_fotos)]
        return self.client.post(ruta, data=data or {}, files=files, headers={**self.H, **(headers or {})})

    def test_umbral_del_cliente_se_ignora(self):
        # Antes: umbral=-1 hacía que cualquier foto verificase como la víctima
        r = self.post("/api/verificar", {"id": "victima", "umbral": "-1"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["match"])
        r = self.post("/api/identificar", {"umbral": "-1"})
        self.assertIsNone(r.json()["id"])
        r = self.client.post("/api/comparar", data={"umbral": "-1"}, headers=self.H,
                             files=[("foto1", ("1.jpg", b"x")), ("foto2", ("2.jpg", b"x"))])
        self.assertEqual(r.json()["umbral"], self.web_app.system.matcher.threshold)

    def test_sin_oraculo_de_similitud(self):
        # Antes: verificar devolvía 'score' e identificar el top-5 con scores (hill-climbing)
        llamadas = []
        original = self.web_app._explicacion
        self.web_app._explicacion = lambda *a, **k: llamadas.append(a) or {"regiones": [], "mapas": [None, None]}
        try:
            v = self.post("/api/verificar", {"id": "victima"}).json()
            i = self.post("/api/identificar").json()
        finally:
            self.web_app._explicacion = original
        self.assertNotIn("score", v)
        self.assertNotIn("ranking", i)
        self.assertNotIn("score", str(i))
        self.assertIsNone(v["explicacion"])  # sin coincidencia no se explica (sería otro oráculo)
        self.assertIsNone(i["explicacion"])
        self.assertEqual(llamadas, [])

    def test_sin_detector_no_vale_para_decidir(self):
        # Antes: modo=sd pasaba la imagen tal cual al extractor, así que se podía fabricar
        # una imagen sintética a partir de la plantilla pública de la víctima
        self.assertEqual(self.post("/api/verificar", {"id": "victima", "modo": "sd"}).status_code, 422)
        self.assertEqual(self.post("/api/identificar", {"modo": "sd"}).status_code, 422)
        self.assertEqual(self.post("/api/identificar", {"modo": "otro"}).status_code, 422)

    def test_inscribir_no_sustituye_sin_confirmar(self):
        # Antes: inscribir con el id de otra persona sustituía su plantilla sin avisar
        guardar = self.web_app._guardar_foto
        self.web_app._guardar_foto = lambda id, data: None
        try:
            r = self.post("/api/inscribir", {"id": "victima"}, campo="fotos", headers={"X-CJAR-Token": CLAVE})
            self.assertEqual(r.status_code, 409)
            guardada = self.web_app.system.db.get_template("victima")
            self.assertGreater(float(np.dot(guardada, self.victima)), 0.99)
            r = self.post("/api/inscribir", {"id": "victima", "reemplazar": "true"}, campo="fotos",
                          headers={"X-CJAR-Token": CLAVE})
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json()["actualizado"])
        finally:
            self.web_app._guardar_foto = guardar
            self.web_app.system.db.save_template("victima", self.victima, reemplazar=True)

    def test_bd_llena_responde_507_y_no_se_queda_ilegible(self):
        # Antes: la escritura no aplicaba los topes de la lectura: la inscripción que los cruzaba respondía 200 y
        # dejaba la BD ilegible (503 en todo). Ahora no escribe, responde 507 (no el 409 de «ya inscrito») y no guarda foto
        from unittest import mock
        import src.database as bd
        w = self.web_app
        guardar = w._guardar_foto
        w._guardar_foto = lambda id, data: self.fail("no debe guardarse la foto si no se inscribe")
        antes = sorted(w.system.db.get_all())
        try:
            with mock.patch.object(bd, "MAX_DB_ROWS", len(antes) + 1):  # cabecera + las personas actuales: no cabe otra
                r = self.post("/api/inscribir", {"id": "no_cabe"}, campo="fotos", headers={"X-CJAR-Token": CLAVE})
            self.assertEqual(r.status_code, 507)
            self.assertEqual(sorted(w.system.db.get_all()), antes)
            self.assertIsNone(w.system.db.get_template("no_cabe"))
            self.assertEqual(self.client.get("/api/info").status_code, 200)
        finally:
            w._guardar_foto = guardar

    def test_borrar_quita_tambien_la_foto(self):
        # Antes: DELETE de " borrable" borraba la plantilla de "borrable" pero no su foto
        self.web_app.system.db.save_template("borrable", _unit(5))
        foto = self.web_app._foto_path("borrable")
        foto.write_bytes(b"jpg")
        try:
            r = self.client.delete("/api/personas/%20borrable", headers={**self.H, "X-CJAR-Token": CLAVE})
            self.assertEqual(r.status_code, 200)
            self.assertIsNone(self.web_app.system.db.get_template("borrable"))
            self.assertFalse(foto.exists())
        finally:
            if foto.exists():
                foto.unlink()

    def test_trabajo_pesado_acotado(self):
        # Antes: los endpoints eran 'async def' con el cálculo síncrono dentro (bloqueaba el
        # event loop entero) y no había límite de peticiones en espera
        import inspect
        for f in (self.web_app.inscribir, self.web_app.verificar, self.web_app.identificar, self.web_app.comparar):
            self.assertFalse(inspect.iscoroutinefunction(f), f.__name__)
        tomadas = 0
        while self.web_app.cola.acquire(blocking=False):
            tomadas += 1
        try:
            self.assertEqual(self.post("/api/identificar").status_code, 503)
            self.assertEqual(self.client.get("/api/info").status_code, 200)  # lo ligero sigue respondiendo
        finally:
            for _ in range(tomadas):
                self.web_app.cola.release()
        self.assertEqual(self.post("/api/identificar").status_code, 200)
        r = self.post("/api/inscribir", {"id": "muchas"}, n_fotos=self.web_app.MAX_FOTOS + 1, campo="fotos",
                      headers={"X-CJAR-Token": CLAVE})
        self.assertEqual(r.status_code, 422)
        self.assertLessEqual(self.web_app.MAX_FOTOS * self.web_app.MAX_UPLOAD_BYTES, 100 * 1024 * 1024)

    def test_info_no_espera_al_trabajo_pesado(self):
        # Antes: /api/info tomaba el lock global y se quedaba esperando mientras durase una
        # inscripción (medido: 9,6 s con 10 fotos)
        import threading
        resultado = []
        with self.web_app.lock:
            h = threading.Thread(target=lambda: resultado.append(self.client.get("/api/info").status_code))
            h.start()
            h.join(timeout=10)
            terminado = not h.is_alive()
        h.join()
        self.assertTrue(terminado)
        self.assertEqual(resultado, [200])

    def test_foto_ligada_al_id_exacto(self):
        # Antes: _foto_path resolvía FOTOS_DIR/'{id}.jpg', así que './v', 'x/../v' o 'V'
        # (montaje sin distinción de mayúsculas) llegaban a la foto de 'v'
        app = self.web_app
        base = app._foto_path("victima_foto")
        self.assertEqual(base.parent, app.FOTOS_DIR)
        for alias in ("./victima_foto", "zz/../victima_foto", "Victima_Foto", "victima_foto "):
            self.assertNotEqual(app._foto_path(alias), base, alias)
        base.write_bytes(b"jpg")
        try:
            self.assertTrue(base.is_file())
            for alias in ("./victima_foto", "zz/../victima_foto", "Victima_Foto"):
                self.assertIsNone(app._cara_inscrita(alias, "completo"), alias)
                self.assertEqual(self.client.get(f"/fotos/{alias}.jpg".replace("/../", "%2F..%2F"),
                                                 headers=ADMIN).status_code in (404, 200), True)
            self.assertEqual(self.client.get("/fotos/Victima_Foto.jpg", headers=ADMIN).status_code, 404)
            self.assertEqual(self.client.get("/fotos/victima_foto.jpg", headers=ADMIN).status_code, 200)
        finally:
            base.unlink()
        # y nada del id llega al nombre del fichero
        self.assertRegex(base.name, r"^[0-9a-f]{64}\.jpg$")

    def test_clave_se_exige_antes_de_leer_el_cuerpo(self):
        # Antes: FastAPI analizaba el multipart entero antes de dar el 401, así que un
        # cuerpo roto sin clave daba 400 (y uno válido de decenas de MB se procesaba antes)
        r = self.client.post("/api/inscribir", content=b"no es multipart",
                             headers={"X-CJAR": "1", "Content-Type": "multipart/form-data; boundary=zzz"})
        self.assertEqual(r.status_code, 401)
        r = self.client.post("/api/inscribir", data={"id": "x"}, files={"fotos": ("a.jpg", b"x")},
                             headers={"X-CJAR": "1"})
        self.assertEqual(r.status_code, 401)
        r = self.client.delete("/api/personas/victima", headers={"X-CJAR": "1"})
        self.assertEqual(r.status_code, 401)

    def test_sin_transfer_encoding_y_tope_por_ruta(self):
        # Antes: el tope miraba solo Content-Length (con CL:1 + Transfer-Encoding: chunked no
        # se aplicaba) y era el de la inscripción (101 MiB) para todas las rutas
        H = {"X-CJAR": "1"}
        r = self.client.post("/api/identificar", headers={**H, "Content-Length": "1", "Transfer-Encoding": "chunked"},
                             content=b"x")
        self.assertEqual(r.status_code, 400)
        r = self.client.delete("/api/personas/x", headers={**H, "Transfer-Encoding": "chunked"})
        self.assertIn(r.status_code, (400, 401))
        limite = self.web_app.LIMITE_RUTA
        for ruta, tope in limite.items():
            self.assertLessEqual(tope, 2 * self.web_app.MAX_UPLOAD_BYTES + 2**20)
            r = self.client.post(ruta, headers={**H, "Content-Length": str(tope + 1)}, content=b"")
            self.assertEqual(r.status_code, 413, ruta)
        self.assertLess(limite["/api/identificar"], self.web_app.MAX_BODY_BYTES // 5)

    def test_cuerpos_en_vuelo_acotados_por_presupuesto(self):
        # Antes: Starlette volcaba a /tmp (tmpfs de 256 MiB) cada subida >1 MiB antes de turno() y nada acotaba
        # el total: ~25 subidas anónimas incompletas lo llenaban y el resto, también el administrador, recibía
        # 400 "error parsing the body". Ahora se reserva el Content-Length declarado en un presupuesto aparte
        # para anónimos y para el administrador.
        w = self.web_app
        original = w.cuerpos
        w.cuerpos = w.PresupuestoCuerpos(anonimo=100_000, admin=300_000)
        try:
            H = {"X-CJAR": "1"}
            foto = {"foto": ("a.jpg", b"x" * 50)}
            # presupuesto anónimo lleno: una subida anónima pequeña se rechaza con 503 y Retry-After
            self.assertTrue(w.cuerpos.reservar(100_000, False))
            r = self.client.post("/api/verificar", data={"id": "persona"}, files=foto, headers=H)
            self.assertEqual(r.status_code, 503)
            self.assertIn("retry-after", r.headers)
            # ...pero una petición con la clave usa el pool del administrador: llega a validar la foto (no 503)
            r = self.client.post("/api/verificar", data={"id": "persona"}, files=foto, headers=ADMIN)
            self.assertNotEqual(r.status_code, 503)
            # al liberar, vuelve a admitirse
            w.cuerpos.liberar(100_000, False)
            r = self.client.post("/api/verificar", data={"id": "persona"}, files=foto, headers=H)
            self.assertNotEqual(r.status_code, 503)
            # una declaración mayor que el presupuesto no se admite aunque el pool esté vacío
            r = self.client.post("/api/verificar", headers={**H, "Content-Length": "200000"}, content=b"")
            self.assertEqual(r.status_code, 503)
            # las peticiones terminadas (con éxito o con error) no dejan bytes reservados
            self.assertEqual(w.cuerpos.usado, {False: 0, True: 0})
        finally:
            w.cuerpos = original

    def test_foto_inscrita_solo_para_el_admin(self):
        # Antes: tras coincidir, explicacion.mapas[1] devolvía a cualquiera la cara de la
        # foto de inscripción, que /fotos reserva al administrador
        app = self.web_app
        cara, expl, cara_original = app._cara, app._explicacion, None
        app._cara = lambda data, modo: (self.victima, np.zeros((112, 112, 3), np.uint8))
        app._explicacion = lambda *a, **k: {"regiones": [], "mapas": ["propia", "inscrita"]}
        try:
            for ruta, datos in (("/api/verificar", {"id": "victima"}), ("/api/identificar", {})):
                anonimo = self.post(ruta, datos).json()
                self.assertEqual(anonimo["explicacion"]["mapas"], ["propia", None], ruta)
                admin = self.post(ruta, datos, headers={"X-CJAR-Token": CLAVE}).json()
                self.assertEqual(admin["explicacion"]["mapas"], ["propia", "inscrita"], ruta)
        finally:
            app._cara, app._explicacion = cara, expl

    def _jpeg(self):
        import cv2
        return cv2.imencode(".jpg", np.full((64, 64, 3), 120, np.uint8))[1].tobytes()

    def _inscribir_real(self, id_, **datos):
        return self.client.post("/api/inscribir", data={"id": id_, **datos},
                                files=[("fotos", ("a.jpg", self._jpeg(), "image/jpeg"))],
                                headers={**self.H, "X-CJAR-Token": CLAVE})

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root escribe en cualquier directorio")
    def test_fotos_sin_permiso_inscribe_igualmente_y_avisa(self):
        # Antes: respondía 500 (la excepción salía de _guardar_foto) y dejaba la plantilla ya guardada
        app = self.web_app
        sin_permiso = tempfile.mkdtemp()
        os.chmod(sin_permiso, 0o555)  # fotos/ de solo lectura para este usuario
        original = app.FOTOS_DIR
        app.FOTOS_DIR = Path(sin_permiso)
        try:
            with self.assertLogs("cjar", "WARNING") as cm:
                r = self._inscribir_real("sin_foto")
        finally:
            app.FOTOS_DIR = original
            os.chmod(sin_permiso, 0o755)
            shutil.rmtree(sin_permiso, ignore_errors=True)
        self.assertEqual(r.status_code, 200)
        self.assertIs(r.json()["foto_guardada"], False)
        self.assertIsNotNone(app.system.db.get_template("sin_foto"))  # coherente: inscrita, sin foto
        self.assertIn("web._guardar_foto falló: PermissionError", " ".join(cm.output))
        app.system.db.delete_template("sin_foto")

    def test_si_falla_la_foto_al_sustituir_se_borra_la_anterior(self):
        # No debe quedarse la cara de la versión vieja junto a la plantilla nueva
        import errno
        app = self.web_app
        tmp = tempfile.mkdtemp()
        original, guardar = app.FOTOS_DIR, app._guardar_foto
        app.FOTOS_DIR = Path(tmp)
        try:
            r = self._inscribir_real("viejo")
            self.assertIs(r.json()["foto_guardada"], True)  # control: con permiso se guarda
            foto = app._foto_path("viejo")
            self.assertTrue(foto.is_file())

            def sin_espacio(id_, data):
                raise OSError(errno.ENOSPC, "No space left on device")

            app._guardar_foto = sin_espacio
            with self.assertLogs("cjar", "WARNING") as cm:
                r = self._inscribir_real("viejo", reemplazar="true")
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json()["actualizado"])
            self.assertIs(r.json()["foto_guardada"], False)
            self.assertFalse(foto.exists())
            self.assertIn("web._guardar_foto falló: OSError", " ".join(cm.output))
        finally:
            app.FOTOS_DIR, app._guardar_foto = original, guardar
            app.system.db.delete_template("viejo")
            shutil.rmtree(tmp, ignore_errors=True)

    def test_id_inexistente_no_se_distingue(self):
        # Antes: un id no inscrito daba 404 y uno inscrito 200 (enumeración)
        r_no = self.post("/api/verificar", {"id": "nadie_con_este_id"})
        r_si = self.post("/api/verificar", {"id": "victima"})
        self.assertEqual(r_no.status_code, r_si.status_code)
        self.assertEqual(set(r_no.json()), set(r_si.json()))
        self.assertFalse(r_no.json()["match"])


if __name__ == "__main__":
    unittest.main()
