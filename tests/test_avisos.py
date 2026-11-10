"""
Tests de los avisos (logger "cjar"): los fallos ya no se quedan en silencio, pero BiometricSystem
sigue sin lanzar excepciones y el mensaje de la excepción se trata con limpiar() (ver src/registro.py
para lo que quita y lo que NO garantiza). No cargan los modelos: usan detector y extractor falsos que
lanzan el error pedido.
"""

import logging
import os
import shutil
import tempfile
import unittest

import numpy as np

from src.biometric_system import BiometricSystem
from src.database import BiometricDatabase
from src.matcher import Matcher
from src.registro import fallo, limpiar


class _Roto:
    """Detector/extractor falso: siempre falla con la excepción dada."""

    def __init__(self, exc):
        self.exc = exc

    def detect_and_align(self, foto):
        raise self.exc

    def extract(self, foto):
        raise self.exc


def _unit(seed):
    t = np.random.default_rng(seed).standard_normal(512)
    return (t / np.linalg.norm(t)).astype(np.float32)


class TestRegistroDeFallos(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.s = BiometricSystem.__new__(BiometricSystem)  # sin cargar los modelos
        self.s.detector = self.s.extractor = _Roto(RuntimeError("no se pudo leer b'FOTOSECRETA' con valor 0.987654321"))
        self.s.matcher = Matcher()
        self.s.db = BiometricDatabase(os.path.join(self.tmp, "db.csv"))
        self.s.db.save_template("persona", _unit(1))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cada_metodo_registra_el_fallo_y_mantiene_el_contrato(self):
        # Antes: el fallo se convertía en False/None sin dejar ninguna pista
        s, foto = self.s, b"FOTOSECRETA"
        mala = "7.1234567;abc"  # plantilla con un número reconocible y un token que no es número
        casos = [
            ("inscribir", lambda: s.inscribir("x", foto), False),
            ("verificar", lambda: s.verificar("persona", foto), False),
            ("identificar", lambda: s.identificar(foto), None),
            ("inscribir_sd", lambda: s.inscribir_sd("x", foto), False),
            ("verificar_sd", lambda: s.verificar_sd("persona", foto), False),
            ("identificar_sd", lambda: s.identificar_sd(foto), None),
            ("inscribir_t", lambda: s.inscribir_t("x", mala), False),
            ("verificar_t", lambda: s.verificar_t("persona", mala), False),
            ("identificar_t", lambda: s.identificar_t(mala), None),
        ]
        for nombre, llamada, esperado in casos:
            with self.assertLogs("cjar", "WARNING") as cm:
                resultado = llamada()
            texto = "\n".join(cm.output)
            self.assertIs(resultado, esperado, nombre)  # ningún método lanza: sigue devolviendo False/None
            self.assertIn(f"BiometricSystem.{nombre} falló", texto)
            self.assertRegex(texto, r"(RuntimeError|ValueError)")
            for secreto in ("FOTOSECRETA", "0.987654321", "7.1234567", "abc"):
                self.assertNotIn(secreto, texto, f"{nombre}: el log contiene {secreto!r}")

    def test_un_resultado_normal_no_es_un_fallo(self):
        # id inexistente, sin coincidencia, id ya existente: respuestas normales, sin WARNING
        with self.assertNoLogs("cjar", "WARNING"):
            self.assertFalse(self.s.verificar_t("nadie", _unit(2)))
            self.assertFalse(self.s.verificar_t("persona", _unit(3)))
            self.assertIsNone(self.s.identificar_t(_unit(4)))
            self.assertFalse(self.s.inscribir_t("persona", _unit(5)))

    def test_limpiar_quita_datos(self):
        casos = {
            "could not convert string to float: '0.123456'": "could not convert string to float: '…'",
            "valor 3.14159 fuera de rango": "valor <num> fuera de rango",
            "overflow con 1e30": "overflow con <num>",
            "hay nan e inf en el vector": "hay <num> e <num> en el vector",
            "no se pudo abrir b'\\xff\\xd8bytes'": "no se pudo abrir b'…'",
            'cadena "secreta" y\nsalto': "cadena '…' y salto",
            "se esperaban 512 valores, se recibieron 128": "se esperaban 512 valores, se recibieron 128",
        }
        for entrada, esperado in casos.items():
            self.assertEqual(limpiar(entrada), esperado)
        self.assertEqual(len(limpiar("x" * 5000)), 200)

    def test_el_mismo_aviso_web_no_se_repite_enseguida(self):
        # Fallos provocados por clientes web: no más de un aviso igual cada pocos segundos
        with self.assertLogs("cjar", "WARNING") as cm:
            for _ in range(5):
                fallo("web.prueba_unica_xyz", ValueError("mismo error"), limitar=True)
        self.assertEqual(len(cm.records), 1)
        with self.assertLogs("cjar", "WARNING") as cm:
            for _ in range(3):
                fallo("web.prueba_unica_xyz", ValueError("mismo error"))  # sin límite en la librería
        self.assertEqual(len(cm.records), 3)


class TestLimitadorPorMetodoYTipo(unittest.TestCase):
    """El limitador de la web cuenta por (método, tipo de excepción), no por el texto del mensaje."""

    def test_variar_el_mensaje_no_salta_el_limite(self):
        # Antes: la clave era el texto completo; 300 mensajes distintos (p. ej. con otro tamaño de
        # imagen cada vez) daban 300 líneas de log
        with self.assertLogs("cjar", "WARNING") as cm:
            for i in range(300):
                fallo("web.prueba_a4", RuntimeError(f"Invalid image size ({i}x{i})"), limitar=True)
        self.assertEqual(len(cm.records), 1)

    def test_otro_metodo_u_otro_tipo_si_se_registran(self):
        with self.assertLogs("cjar", "WARNING") as cm:
            fallo("web.prueba_a4_b", RuntimeError("x"), limitar=True)
            fallo("web.prueba_a4_b", RuntimeError("otro texto"), limitar=True)   # mismo (método, tipo): no
            fallo("web.prueba_a4_b", ValueError("x"), limitar=True)              # otro tipo: sí
            fallo("web.prueba_a4_c", RuntimeError("x"), limitar=True)            # otro método: sí
        self.assertEqual(len(cm.records), 3)

    def test_pasado_el_tiempo_vuelve_a_avisar(self):
        from unittest import mock
        from src import registro
        with self.assertLogs("cjar", "WARNING") as cm:
            fallo("web.prueba_a4_d", RuntimeError("x"), limitar=True)
            fallo("web.prueba_a4_d", RuntimeError("y"), limitar=True)
            with mock.patch.object(registro.time, "monotonic", return_value=registro.time.monotonic() + 6):
                fallo("web.prueba_a4_d", RuntimeError("z"), limitar=True)
        self.assertEqual(len(cm.records), 2)


class TestAvisosEnLaWeb(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        import web.app as web_app

        cls.web_app = web_app
        cls.client = TestClient(web_app.app, base_url="http://localhost")
        cls.CLAVE = "clave-de-prueba-registro-0123"
        cls.clave_original = web_app.ADMIN_TOKEN
        web_app.ADMIN_TOKEN = cls.CLAVE

    @classmethod
    def tearDownClass(cls):
        cls.web_app.ADMIN_TOKEN = cls.clave_original

    def test_los_avisos_salen_con_formato_en_el_log_del_servidor(self):
        # Antes: nada configuraba el logger "cjar" en la web
        log = logging.getLogger("cjar")
        self.assertTrue(any(isinstance(h, logging.StreamHandler) for h in log.handlers))
        formato = log.handlers[0].formatter._fmt
        self.assertIn("%(levelname)s", formato)
        self.assertIn("%(name)s", formato)

    def test_un_fallo_inesperado_de_la_web_se_registra_sin_la_clave(self):
        app = self.web_app
        original = app.system.detector.detect_and_align

        def roto(_foto):
            raise RuntimeError("fallo interno con valor 0.55555")

        app.system.detector.detect_and_align = roto
        try:
            with self.assertLogs("cjar", "WARNING") as cm:
                r = self.client.post("/api/identificar", files={"foto": ("a.jpg", b"x")}, headers={"X-CJAR": "1"})
                self.client.post("/api/inscribir", data={"id": "x"}, files={"fotos": ("a.jpg", b"x")},
                                 headers={"X-CJAR": "1", "X-CJAR-Token": self.CLAVE})
        finally:
            app.system.detector.detect_and_align = original
        self.assertEqual(r.status_code, 422)  # la respuesta al cliente no cambia
        texto = "\n".join(cm.output)
        self.assertIn("web._cara falló: RuntimeError", texto)
        self.assertNotIn("0.55555", texto)
        self.assertNotIn(self.CLAVE, texto)



class TestLimpiarRecorte(unittest.TestCase):
    """limpiar() recorta ANTES de las expresiones regulares: una racha larga de dígitos ya no cuesta segundos."""

    def test_miles_de_digitos_seguidos_no_tardan_segundos(self):
        # Antes: la expresión de los números era cuadrática (30.000 dígitos: unos 5 s; 40.000: 11 s)
        import time
        for n in (30_000, 200_000):
            t0 = time.perf_counter()
            salida = limpiar("1" * n)
            self.assertLess(time.perf_counter() - t0, 1.0, f"{n} dígitos")
            self.assertLessEqual(len(salida), 200)
        t0 = time.perf_counter()
        fallo("prueba_rendimiento", ValueError("7" * 30_000))  # el camino real: dentro del except
        self.assertLess(time.perf_counter() - t0, 1.0)

    def test_el_recorte_no_deja_a_medias_un_texto_entre_comillas(self):
        # Si el recorte cayera dentro de unas comillas, su principio se vería: no debe ocurrir
        mensaje = "x" * 50 + " '" + "SECRETO" * 200 + "' fin"
        self.assertGreater(len(mensaje), 500)
        salida = limpiar(mensaje)
        self.assertNotIn("SECRETO", salida)
        self.assertLessEqual(len(salida), 200)
        mensaje = "y" * 100 + ' "' + "CLAVE" * 300  # comillas dobles que ni siquiera llegan a cerrarse
        self.assertNotIn("CLAVE", limpiar(mensaje))

    def test_los_caracteres_de_control_se_sustituyen(self):
        # Antes: ESC, NUL, retroceso y DEL pasaban tal cual al log (una secuencia ANSI limpia o colorea una terminal)
        controles = [chr(27), chr(0), chr(8), chr(127), chr(7), chr(0x9b), chr(0x202e), chr(0x200b), chr(0xfeff)]
        mensaje = "a" + "".join(controles) + chr(27) + "[2J" + chr(27) + "[31mROJO" + chr(27) + "[0m fin"
        salida = limpiar(mensaje)
        for c in controles:
            self.assertNotIn(c, salida, hex(ord(c)))
        self.assertTrue(salida.startswith("a?"))
        self.assertIn("ROJO", salida)  # el texto normal se conserva
        self.assertTrue(all(ord(c) >= 32 and ord(c) != 127 for c in salida))
        # y lo que ya funcionaba sigue funcionando: los saltos de línea se colapsan en un espacio
        saltos = "linea1" + chr(10) + "INFO cjar: FALSA" + chr(13) + chr(10) + "l3" + chr(0x2028) + "l4" + chr(0x2029) + "l5" + chr(0x85) + "l6"
        self.assertEqual(limpiar(saltos), "linea1 INFO cjar: FALSA l3 l4 l5 l6")

    def test_el_aviso_completo_no_lleva_controles(self):
        with self.assertLogs("cjar", "WARNING") as cm:
            fallo("prueba_control", RuntimeError("fallo" + chr(27) + "[31m" + chr(0) + chr(127)))
        self.assertEqual(cm.output[0].count(chr(27)) + cm.output[0].count(chr(0)) + cm.output[0].count(chr(127)), 0)
        self.assertIn("fallo?[31m??", cm.output[0])

    def test_los_mensajes_normales_no_cambian(self):
        self.assertEqual(limpiar("could not convert string to float: 'abc'"), "could not convert string to float: '…'")
        self.assertEqual(limpiar("can't convert 3.5 to int"), "can't convert <num> to int")  # una sola comilla suelta, mensaje corto
        self.assertEqual(limpiar("se esperaban 512 valores, se recibieron 128"), "se esperaban 512 valores, se recibieron 128")
        self.assertEqual(limpiar(ValueError("a\nb\r\nc")), "a b c")


@unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root puede escribir en cualquier directorio")
class TestComprobacionAlArrancar(unittest.TestCase):
    """Un directorio de BD que no se puede escribir se avisa al arrancar, sin lanzar ni parar."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: (os.chmod(self.tmp, 0o755), shutil.rmtree(self.tmp, ignore_errors=True)))

    def test_directorio_de_la_bd_no_escribible_avisa_con_la_solucion(self):
        # Antes: no se decía nada; el único síntoma era que inscribir devolvía False
        ruta = os.path.join(self.tmp, "database.csv")
        BiometricDatabase(ruta).save_template("persona", _unit(1))  # CSV existente, así leer sigue siendo posible
        os.chmod(self.tmp, 0o555)  # de solo lectura para este usuario
        with self.assertLogs("cjar", "WARNING") as cm:
            db = BiometricDatabase(ruta)  # no lanza
        texto = "\n".join(cm.output)
        self.assertIn(os.path.realpath(self.tmp).replace("\\", "/")[-20:], texto.replace("\\", "/"))
        self.assertIn("no es escribible", texto)
        self.assertIn("chown 1000:1000 database fotos", texto)
        self.assertIn("CJAR_UID", texto)
        self.assertIsNotNone(db.get_template("persona"))  # leer sigue funcionando
        self.assertFalse(BiometricSystem_inscribir_t(db, "otra"))  # y escribir falla, ya con el motivo en el log

    def test_directorio_escribible_no_avisa(self):
        with self.assertNoLogs("cjar", "WARNING"):
            BiometricDatabase(os.path.join(self.tmp, "database.csv"))

    def test_el_aviso_no_se_repite_por_cada_instancia(self):
        os.chmod(self.tmp, 0o555)
        ruta = os.path.join(self.tmp, "x", "database.csv")  # no se puede crear: el directorio padre es de solo lectura
        from src.registro import avisar_si_no_escribible
        with self.assertLogs("cjar", "WARNING") as cm:
            for _ in range(3):
                self.assertFalse(avisar_si_no_escribible(self.tmp, "inscribir ni borrar personas"))
        self.assertEqual(len(cm.records), 1)

    def test_directorio_inexistente_avisa_y_no_lanza(self):
        from src.registro import avisar_si_no_escribible
        with self.assertLogs("cjar", "WARNING") as cm:
            self.assertFalse(avisar_si_no_escribible(os.path.join(self.tmp, "no_existe"), "guardar las fotos"))
        self.assertIn("no existe", cm.output[0])

    def test_la_web_comprueba_tambien_el_directorio_de_fotos(self):
        import inspect
        import web.app as web_app
        fuente = inspect.getsource(web_app)
        self.assertIn('avisar_si_no_escribible(str(FOTOS_DIR)', fuente)
        self.assertLess(fuente.index("configurar_para_servidor()"), fuente.index("avisar_si_no_escribible(str(FOTOS_DIR)"))


def BiometricSystem_inscribir_t(db, id_):
    """inscribir_t sobre una BD concreta, con el contrato de BiometricSystem (False si falla)."""
    s = BiometricSystem.__new__(BiometricSystem)
    s.db, s.matcher = db, Matcher()
    with unittest.TestCase().assertLogs("cjar", "WARNING"):
        return s.inscribir_t(id_, _unit(9))


if __name__ == "__main__":
    unittest.main()


class TestAvisosDeLaBaseDeDatos(unittest.TestCase):
    """Al leer el CSV: filas válidas/descartadas, ids duplicados y bajada de personas, sin inundar el log."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.ruta = os.path.join(self.tmp, "database.csv")
        db = BiometricDatabase(self.ruta)
        for i, uid in enumerate(("ana", "beto", "carla"), 1):
            db.save_template(uid, _unit(i))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _anadir(self, datos: bytes):
        with open(self.ruta, "ab") as f:
            f.write(datos)

    def _fila(self, uid, semilla):
        return (uid + "," + ";".join(f"{v:.7f}" for v in _unit(semilla)) + "\r\n").encode()

    def test_lectura_sana_un_resumen_una_sola_vez(self):
        # Antes: el CSV se leía sin decir nada
        db = BiometricDatabase(self.ruta)
        with self.assertLogs("cjar", "INFO") as cm:
            for _ in range(4):
                db.get_all()
                db.get_template("ana")
                db.get_template("nadie")
        self.assertEqual(len(cm.records), 1)  # no una por petición
        self.assertEqual(cm.records[0].levelno, logging.INFO)
        self.assertIn("3 filas válidas (3 personas), 0 descartadas", cm.output[0])

    def test_filas_descartadas_e_ids_duplicados_son_un_warning(self):
        self._anadir(b"basura sin coma\r\n" + b"vacio,\r\n" + "mu\xf1oz,1;2\r\n".encode("latin-1"))
        self._anadir(self._fila("beto", 7))  # el mismo id otra vez: gana la última fila
        db = BiometricDatabase(self.ruta)
        with self.assertLogs("cjar", "WARNING") as cm:
            db.get_all()
        texto = "\n".join(cm.output)
        self.assertIn("4 filas válidas (3 personas), 3 descartadas", texto)
        self.assertIn("ids duplicados (gana la última fila): beto", texto)
        # y el aviso no repite el contenido de las filas descartadas ni valores de plantillas
        for dato in ("basura", "vacio", "1;2", "0.5", "0.1"):
            self.assertNotIn(dato, texto)

    def test_aviso_si_baja_el_numero_de_personas(self):
        db = BiometricDatabase(self.ruta)
        self.assertEqual(len(db.get_all()), 3)
        os.truncate(self.ruta, len("id,template\r\n"))  # alguien trunca el CSV con la web en marcha
        with self.assertLogs("cjar", "WARNING") as cm:
            self.assertEqual(db.get_all(), {})
        self.assertIn("ha bajado de 3 a 0", cm.output[0])
        self.assertIn("posible truncado o borrado", cm.output[0])
        # lo mismo si borran el fichero
        db2 = BiometricDatabase(self.ruta)
        db2.save_template("dani", _unit(4))
        self.assertEqual(len(db2.get_all()), 1)
        os.unlink(self.ruta)
        with self.assertLogs("cjar", "WARNING") as cm:
            db2.get_all()
        self.assertIn("ha bajado de 1 a 0", cm.output[0])

    def test_borrar_a_proposito_no_es_una_bajada(self):
        db = BiometricDatabase(self.ruta)
        db.get_all()
        with self.assertNoLogs("cjar", "WARNING"):
            self.assertTrue(db.delete_template("ana"))
            self.assertEqual(len(db.get_all()), 2)
            db.save_template("dani", _unit(4))
            self.assertEqual(len(db.get_all()), 3)

    def test_un_aviso_por_cada_cambio_real_del_fichero(self):
        db = BiometricDatabase(self.ruta)
        with self.assertLogs("cjar", "WARNING") as cm:
            self._anadir(b"sin coma 1\r\n")
            for _ in range(3):
                db.get_all()
                db.get_template("ana")
            self._anadir(b"sin coma 2\r\n")  # otro cambio real
            for _ in range(3):
                db.get_all()
                db.get_template("ana")
        self.assertEqual(len(cm.records), 2)
        self.assertIn("1 descartadas", cm.output[0])
        self.assertIn("2 descartadas", cm.output[1])

    def test_bd_ilegible_se_avisa_una_vez_y_sigue_fallando_cerrado(self):
        from unittest import mock
        from src.database import DatabaseReadError
        db = BiometricDatabase(self.ruta)
        with mock.patch("src.database.MAX_DB_ROWS", 2):
            with self.assertLogs("cjar", "WARNING") as cm:
                for _ in range(5):
                    with self.assertRaises(DatabaseReadError):  # el comportamiento no cambia: falla cerrado
                        db.get_template("ana")
        self.assertEqual(len(cm.records), 1)
        self.assertIn("demasiadas filas", cm.output[0])

    def test_los_ids_del_aviso_pasan_por_limpiar(self):
        # Antes: los ids salían tal cual; parse_input_id acepta U+2028/U+2029 (no son de control) y llegaban crudos al log
        raro = "a" + chr(0x2028) + "INFO falso" + chr(0x2029) + "fin"
        self._anadir(self._fila(raro, 7) + self._fila(raro, 8))  # duplicado
        fin_de_linea = bytes([13, 10])
        self._anadir(("zeta" + chr(0x2028) + "2,").encode() + b";".join([b"nan"] * 512) + fin_de_linea)  # plantilla inválida
        db = BiometricDatabase(self.ruta)
        with self.assertLogs("cjar", "WARNING") as cm:
            db.get_all()
        texto = chr(10).join(cm.output)
        self.assertIn("ids duplicados (gana la última fila): a INFO falso fin", texto)
        self.assertIn("plantilla que no es válida", texto)
        for c in (chr(0x2028), chr(0x2029)):
            self.assertNotIn(c, texto)
        self.assertEqual(len([l for l in texto.split(chr(10)) if l]), 2)  # y no se parte en líneas nuevas

    def test_los_ids_largos_o_con_comillas_no_rompen_el_aviso(self):
        largo = "L" * 128
        self._anadir(self._fila(largo, 7) + self._fila(largo, 8))
        self._anadir(self._fila("o'neil", 9) + self._fila("o'neil", 10))
        db = BiometricDatabase(self.ruta)
        with self.assertLogs("cjar", "WARNING") as cm:
            db.get_all()
        self.assertEqual(len(cm.records), 1)
        self.assertIn("L" * 128, cm.output[0])  # un id de 128 caracteres cabe entero

    def test_plantilla_con_formato_invalido_se_avisa_en_get_all(self):
        self._anadir(b"zeta," + b";".join([b"nan"] * 512) + b"\r\n")
        db = BiometricDatabase(self.ruta)
        with self.assertLogs("cjar", "WARNING") as cm:
            todos = db.get_all()
            db.get_all()
        self.assertNotIn("zeta", todos)
        self.assertEqual(sum("plantilla que no es válida" in m for m in cm.output), 1)
        self.assertIn("zeta", "\n".join(cm.output))


class TestFalloNuncaLanza(unittest.TestCase):
    """Registrar un fallo no puede romper el contrato de BiometricSystem (solo devuelve False/None)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.s = BiometricSystem.__new__(BiometricSystem)
        self.s.matcher = Matcher()
        self.s.db = BiometricDatabase(os.path.join(self.tmp, "db.csv"))
        self.s.db.save_template("persona", _unit(1))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_excepcion_con_str_que_lanza_no_rompe_el_contrato(self):
        # Antes: fallo() llamaba a str(exc) sin proteger y la excepción nueva salía del método
        class StrRoto(Exception):
            def __str__(self):
                raise RuntimeError("__str__ roto")

        class StrNoTexto(Exception):
            def __str__(self):
                return 12345  # devuelve algo que no es str: str() lanza TypeError

        for exc in (StrRoto(), StrNoTexto()):
            self.s.detector = self.s.extractor = _Roto(exc)
            with self.assertLogs("cjar", "WARNING") as cm:
                self.assertIs(self.s.verificar_sd("persona", b"x"), False)
                self.assertIs(self.s.inscribir("x", b"x"), False)
                self.assertIsNone(self.s.identificar(b"x"))
            texto = "\n".join(cm.output)
            self.assertIn(type(exc).__name__, texto)        # al menos queda el tipo de la excepción
            self.assertIn("mensaje no disponible", texto)

    def test_fallo_directo_no_lanza_con_objetos_raros(self):
        class Meta(type):
            @property
            def __name__(cls):  # type(exc).__name__ lanza al consultarlo
                raise RuntimeError("sin nombre")

        class SinNombre(Exception, metaclass=Meta):
            pass

        fallo("prueba", SinNombre("x"))                # no lanza
        fallo("prueba", SinNombre("x"), limitar=True)  # tampoco con el limitador
        fallo(None, ValueError("x"))
