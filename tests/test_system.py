"""
Tests del sistema: funcionamiento con templates sintéticos y los ataques del enunciado
(templates inválidos, CSV editado a mano, ids maliciosos, bombas de imagen...).
Usan una BD temporal, no la oficial.
"""

import os
import shutil
import tempfile
import unittest
import numpy as np
from src.biometric_system import BiometricSystem


class TestBiometricSystemAttacks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # BD en un directorio temporal: antes era database/test_database.csv (en el volumen real), que se
        # borraba al empezar y al acabar y dejaba un .lock huérfano
        cls.tmp = tempfile.mkdtemp()
        cls.test_db_path = os.path.join(cls.tmp, "test_database.csv")
        cls.system = BiometricSystem(db_path=cls.test_db_path, threshold=0.48)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _generate_valid_template(self, seed: int) -> np.ndarray:
        rng = np.random.default_rng(seed)
        t = rng.standard_normal(512).astype(np.float32)
        return t / np.linalg.norm(t)

    # 1. Pruebas Funcionales Base (_t)
    def test_01_enroll_and_verify_t_valid(self):
        t1 = self._generate_valid_template(101)
        self.assertTrue(self.system.inscribir_t("user_101", t1))
        self.assertTrue(self.system.verificar_t("user_101", t1))
        t_distinto = self._generate_valid_template(999)
        self.assertFalse(self.system.verificar_t("user_101", t_distinto))

    def test_02_identify_t_valid(self):
        t2 = self._generate_valid_template(202)
        self.system.inscribir_t("user_202", t2)
        identified_id = self.system.identificar_t(t2)
        self.assertEqual(identified_id, "user_202")

    # 2. Ataque: Inyección de Templates Maliciosos (_t)
    def test_03_attack_wrong_template_dimensions(self):
        # Template con 128 valores en vez de 512
        bad_template_short = np.random.rand(128).astype(np.float32)
        self.assertFalse(self.system.inscribir_t("attacker_short", bad_template_short))
        self.assertFalse(self.system.verificar_t("user_101", bad_template_short))
        self.assertIsNone(self.system.identificar_t(bad_template_short))

    def test_04_attack_null_and_nan_vectors(self):
        # Vector con todo ceros (norma nula)
        zero_template = np.zeros(512, dtype=np.float32)
        self.assertFalse(self.system.inscribir_t("attacker_zero", zero_template))

        # Vector con NaNs e infinitos
        nan_template = self._generate_valid_template(303)
        nan_template[5] = np.nan
        self.assertFalse(self.system.inscribir_t("attacker_nan", nan_template))
        self.assertFalse(self.system.verificar_t("user_101", nan_template))

    # 3. Ataque: Manipulación Directa del Archivo CSV
    def test_05_attack_tampered_csv_file(self):
        # Simulamos que el atacante abrió y escribió basura directa en el CSV
        with open(self.test_db_path, "a", encoding="utf-8") as f:
            f.write("attacker_corrupted,esto_no_es_un_vector_valido\n")
            f.write("attacker_empty_vector,\n")
            f.write(",1.0;2.0;3.0\n")

        # El sistema no debe romperse al cargar la BD manipulada
        valid_records = self.system.db.get_all()
        self.assertNotIn("attacker_corrupted", valid_records)
        self.assertNotIn("attacker_empty_vector", valid_records)

        # Las identidades legítimas previas deben seguir verificando sin problemas
        t1 = self._generate_valid_template(101)
        self.assertTrue(self.system.verificar_t("user_101", t1))

    # 4. Trazabilidad: explicación por zonas de la cara
    def test_06_explicador_zonas(self):
        from src.explicador import Explicador, REGIONES
        cara = np.random.default_rng(6).integers(0, 256, (112, 112, 3), dtype=np.uint8)
        t = self.system.extractor.extract(cara)
        r = Explicador(self.system.extractor).explicar(cara, t, cara, t)

        aportes = [z["aporte"] for z in r["regiones"]]
        self.assertEqual(len(aportes), len(REGIONES))
        self.assertTrue(np.all(np.isfinite(aportes)))
        self.assertEqual(aportes, sorted(aportes, reverse=True))
        # Contra su propio template la similitud base es 1: tapar una zona solo puede bajarla
        self.assertTrue(all(a >= -1e-4 for a in aportes))
        self.assertEqual([m.shape for m in r["mapas"]], [(224, 224, 3)] * 2)

    # 5. Ataques: desbordamiento, ids maliciosos, bombas de imagen
    def test_07_attack_overflow_template(self):
        # Con 1e30 la norma en float32 era infinito y se guardaba un vector de ceros
        # y se daba por bueno. Ahora se normaliza bien: queda un template válido de norma 1
        for uid, t in [("enorme", [1e30] * 512), ("maximo", np.full(512, 3e38, np.float32)),
                       ("diminuto", np.full(512, 1e-30))]:
            self.assertTrue(self.system.inscribir_t(uid, t))
            guardado = self.system.db.get_template(uid)
            self.assertIsNotNone(guardado, uid)
            self.assertAlmostEqual(float(np.linalg.norm(guardado)), 1.0, places=4)
            self.assertTrue(self.system.verificar_t(uid, t))
        # Lo que desborda incluso en float64 (infinito) se sigue rechazando
        self.assertFalse(self.system.inscribir_t("infinito", [1e400] * 512))

    def test_08_attack_malicious_ids(self):
        t = self._generate_valid_template(808)
        for mal in ["", "   ", "a\nb", "nulo\x00", "=HYPERLINK(\"http://x\")", "+1", "@SUM(A1)",
                    "x" * 500, None, 3.5, ["lista"], b"bytes"]:
            self.assertFalse(self.system.inscribir_t(mal, t), repr(mal))
        self.assertNotIn("a\nb", self.system.db.get_all())
        # Los ids normales (también con paréntesis o guiones en medio) siguen funcionando
        self.assertTrue(self.system.inscribir_t("aitana_(twin_melody)", t))
        self.assertTrue(self.system.inscribir_t(42, t))
        self.assertTrue(self.system.verificar_t("42", t))

    def test_09_attack_image_bombs_and_bad_arrays(self):
        import struct, zlib

        def chunk(tipo, datos):
            return struct.pack(">I", len(datos)) + tipo + datos + struct.pack(">I", zlib.crc32(tipo + datos))

        # PNG de unos pocos bytes que declara 60000 x 60000 píxeles
        bomba = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 60000, 60000, 8, 2, 0, 0, 0))
                 + chunk(b"IDAT", zlib.compress(b"")) + chunk(b"IEND", b""))
        self.assertFalse(self.system.verificar_sd("user_101", bomba))
        self.assertIsNone(self.system.identificar(bomba))

        con_nan = np.full((112, 112, 3), np.nan)
        self.assertIsNone(self.system.identificar_sd(con_nan))
        self.assertIsNone(self.system.identificar_sd(np.zeros((112, 112, 3), dtype=complex)))
        self.assertIsNone(self.system.identificar_sd(np.zeros((112, 112, 3), dtype=object)))
        self.assertFalse(self.system.verificar_sd("user_101", "x" * 10_000_000))

    def test_10_attack_oversized_template_inputs(self):
        self.assertFalse(self.system.inscribir_t("grande", ";".join(["0.1"] * 2_000_000)))
        self.assertFalse(self.system.inscribir_t("grande", [0.1] * 2_000_000))
        self.assertIsNone(self.system.identificar_t(np.zeros(10_000_000, np.float32)))
        self.assertIsNone(self.system.identificar_t(np.array(["0.1"] * 512, dtype=object)))

    def test_12_limite_de_pixeles_de_opencv(self):
        # Antes: web/app.py y src/detector.py importaban cv2 antes de fijar
        # OPENCV_IO_MAX_IMAGE_PIXELS, así que OpenCV decodificaba imágenes de 49 Mpx
        import subprocess, sys
        prueba = ("import {mod}, cv2, numpy as np\n"
                  "png = cv2.imencode('.png', np.zeros((7000, 7000, 3), np.uint8))[1]\n"
                  "try:\n    d = cv2.imdecode(png, cv2.IMREAD_COLOR)\nexcept cv2.error:\n    d = None\n"
                  "print('decodificada' if d is not None else 'rechazada')\n")
        for mod, entorno in [("web", {}), ("src.detector", {}), ("src", {"OPENCV_IO_MAX_IMAGE_PIXELS": "9999999999"})]:
            env = {k: v for k, v in os.environ.items() if k != "OPENCV_IO_MAX_IMAGE_PIXELS"} | entorno
            out = subprocess.run([sys.executable, "-c", prueba.format(mod=mod)], capture_output=True, text=True,
                                 env=env, timeout=120).stdout.strip().splitlines()
            self.assertEqual(out[-1:], ["rechazada"], (mod, entorno))

    # 6. Suplantación por reinscripción
    def test_11_inscribir_no_sustituye_a_otro(self):
        # Antes: inscribir_t con el id de otra persona sustituía su plantilla en silencio
        victima, atacante = self._generate_valid_template(1101), self._generate_valid_template(1102)
        self.assertTrue(self.system.inscribir_t("victima_11", victima))
        self.assertFalse(self.system.inscribir_t("victima_11", atacante))
        self.assertFalse(self.system.verificar_t("victima_11", atacante))
        self.assertTrue(self.system.verificar_t("victima_11", victima))
        # El operador puede sustituirla a propósito
        self.assertTrue(self.system.inscribir_t("victima_11", atacante, reemplazar=True))
        self.assertTrue(self.system.verificar_t("victima_11", atacante))


class TestDatabaseIntegrity(unittest.TestCase):
    """Un fallo de lectura o una fila ilegible no pueden acabar borrando la BD."""

    def setUp(self):
        import tempfile
        from src.database import BiometricDatabase
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "db.csv")
        self.db = BiometricDatabase(self.path)
        rng = np.random.default_rng(7)
        for uid in ("alice", "bob", "carol"):
            self.db.save_template(uid, rng.standard_normal(512))
        self.nuevo = rng.standard_normal(512)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def _bytes(self):
        with open(self.path, "rb") as f:
            return f.read()

    def test_lectura_fallida_no_reescribe(self):
        # Antes: _read_raw_records hacía 'except Exception: return {}' y save_template
        # reescribía la BD solo con el registro nuevo
        import builtins
        from unittest import mock
        from src.database import DatabaseReadError
        antes = self._bytes()
        real_open = builtins.open

        def open_bloqueado(path, mode="r", *a, **k):
            if str(path) == self.path and "r" in mode:
                raise PermissionError("bloqueado por otro programa")
            return real_open(path, mode, *a, **k)

        with mock.patch("builtins.open", open_bloqueado):
            with self.assertRaises(DatabaseReadError):
                self.db.save_template("dave", self.nuevo)
            with self.assertRaises(DatabaseReadError):
                self.db.get_all()
        self.assertEqual(self._bytes(), antes)
        self.assertEqual(sorted(self.db.get_all()), ["alice", "bob", "carol"])

    def test_byte_no_utf8_solo_invalida_su_fila(self):
        # Antes: un byte 0xFF (p. ej. un CSV guardado en ANSI por Excel) vaciaba la BD
        with open(self.path, "ab") as f:
            f.write("mu\xf1oz,1;2\r\n".encode("latin-1"))
        self.assertEqual(sorted(self.db.get_all()), ["alice", "bob", "carol"])
        self.assertTrue(self.db.save_template("dave", self.nuevo))
        self.assertEqual(sorted(self.db.get_all()), ["alice", "bob", "carol", "dave"])
        self.assertIn("mu\xf1oz,1;2".encode("latin-1"), self._bytes())  # se conserva tal cual

    def test_campo_enorme_solo_invalida_su_fila(self):
        with open(self.path, "a", encoding="utf-8") as f:
            f.write("zed," + "x" * 140_000 + "\n")
        self.assertTrue(self.db.save_template("dave", self.nuevo))
        self.assertEqual(sorted(self.db.get_all()), ["alice", "bob", "carol", "dave"])

    def test_escritores_concurrentes_no_pierden_registros(self):
        # Antes: dos instancias (p. ej. la web y cargar_dataset.py) podían pisarse al
        # leer-modificar-escribir y compartían el temporal fijo database.csv.tmp
        import threading
        from src.database import BiometricDatabase
        otra = BiometricDatabase(self.path)
        rng = np.random.default_rng(8)
        t = rng.standard_normal(512)
        barrera = threading.Barrier(2)

        def escribir(db, prefijo):
            barrera.wait()
            for i in range(25):
                db.save_template(f"{prefijo}{i}", t)

        hilos = [threading.Thread(target=escribir, args=(d, p)) for d, p in ((self.db, "a"), (otra, "b"))]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join()
        self.assertEqual(len(self.db.get_all()), 3 + 50)

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0, "solo tiene sentido sin root")
    def test_lock_de_otro_usuario_no_bloquea(self):
        # Un .lock que dejó una versión anterior que corría como root (sin permiso de escritura)
        lock = self.path + ".lock"
        open(lock, "a").close()
        os.chmod(lock, 0o444)
        self.assertTrue(self.db.save_template("dave", self.nuevo))

    @unittest.skipUnless(hasattr(os, "symlink") and os.name == "posix", "necesita enlaces simbólicos POSIX")
    def test_temporal_no_sigue_enlaces(self):
        victima = os.path.join(self.dir, "victima.txt")
        with open(victima, "w") as f:
            f.write("no tocar")
        os.symlink(victima, self.path + ".tmp")
        self.db.save_template("dave", self.nuevo)
        with open(victima) as f:
            self.assertEqual(f.read(), "no tocar")
        self.assertFalse(os.path.islink(self.path))

    def _fila(self, uid):
        t = ";".join(f"{v:.7f}" for v in np.random.default_rng(3).standard_normal(512))
        return f"{uid},{t}\r\n"

    def test_ids_del_csv_se_validan_como_al_inscribir(self):
        # Antes: _load aceptaba '-planted' o un id de 200 caracteres: identificar los
        # devolvía pero no se podían borrar ni sustituir
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(self._fila("-planted") + self._fila("=cmd") + self._fila("L" * 200))
        self.assertEqual(sorted(self.db.get_all()), ["alice", "bob", "carol"])
        self.assertTrue(self.db.delete_template("bob"))  # reescribe
        self.assertEqual(sorted(self.db.get_all()), ["alice", "carol"])
        with open(self.path, "rb") as f:
            datos = f.read()
        for uid in (b"-planted", b"=cmd"):  # se conservan en bruto, sin perder datos
            self.assertIn(uid + b",", datos)

    def test_csv_sin_cabecera_no_pierde_la_primera_fila(self):
        # Antes: la primera línea se descartaba siempre y el siguiente guardado la borraba
        with open(self.path, "r", encoding="utf-8") as f:
            lineas = f.read().splitlines()
        with open(self.path, "w", encoding="utf-8", newline="") as f:
            f.write("\r\n".join(lineas[1:]) + "\r\n")  # sin cabecera
        self.assertEqual(sorted(self.db.get_all()), ["alice", "bob", "carol"])
        self.db.save_template("dave", self.nuevo)
        self.assertEqual(sorted(self.db.get_all()), ["alice", "bob", "carol", "dave"])

    def test_cabecera_con_bom_se_reconoce(self):
        with open(self.path, "rb") as f:
            datos = f.read()
        with open(self.path, "wb") as f:
            f.write(b"\xef\xbb\xbf" + datos)
        self.assertEqual(sorted(self.db.get_all()), ["alice", "bob", "carol"])

    def test_save_template_no_sustituye_por_defecto(self):
        # Antes: save_template sustituía por defecto, al revés que BiometricSystem.inscribir*
        self.assertFalse(self.db.save_template("alice", self.nuevo))
        self.assertTrue(self.db.save_template("alice", self.nuevo, reemplazar=True))

    def test_get_all_usa_cache_hasta_que_cambia_el_fichero(self):
        from unittest import mock
        self.db.get_all()
        with mock.patch.object(self.db, "_read_raw_records", side_effect=AssertionError("no debía releer")):
            self.assertEqual(len(self.db.get_all()), 3)
        self.db.save_template("dave", self.nuevo)  # cambia el fichero: se invalida
        self.assertEqual(len(self.db.get_all()), 4)
        copia = self.db.get_all()
        copia.clear()  # lo devuelto es una copia: no corrompe la caché
        self.assertEqual(len(self.db.get_all()), 4)

    def test_ids_con_barra_no_entran(self):
        # Antes: parse_input_id aceptaba 'a/b'; la web no puede borrar ni consultar un id así
        # (/api/personas/{id} es de un solo segmento) y un CSV ajeno lo dejaba irrevocable
        from src.utils import parse_input_id
        for mal in ("a/b", "..", ".", "a\\b", "../x"):
            with self.assertRaises(ValueError, msg=mal):
                parse_input_id(mal)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(self._fila("x/y"))
        self.assertEqual(sorted(self.db.get_all()), ["alice", "bob", "carol"])
        self.assertTrue(self.db.delete_template("bob"))
        with open(self.path, "rb") as f:
            self.assertIn(b"x/y,", f.read())  # se conserva en bruto
        with self.assertRaises(ValueError):
            self.db.save_template("a/b", self.nuevo)
        self.assertEqual(parse_input_id("a.b-c (d)"), "a.b-c (d)")

    def test_tope_de_filas_y_lectura_cacheada(self):
        # Antes: get_template reparseaba todo el CSV en cada verificación y no había tope de
        # filas (1M de filas basura = ~2 s por petición, bajo el lock global)
        from unittest import mock
        from src.database import DatabaseReadError, MAX_DB_ROWS
        with mock.patch.object(self.db, "_leer", wraps=self.db._leer) as leer:
            for _ in range(3):
                self.assertIsNotNone(self.db.get_template("alice"))
                self.db.get_all()
            self.assertEqual(leer.call_count, 1)
        antes = self._bytes()
        with open(self.path, "ab") as f:
            f.write(b"a,\r\n" * (MAX_DB_ROWS + 1))
        with self.assertRaises(DatabaseReadError):
            self.db.get_template("alice")
        self.assertEqual(self._bytes()[:len(antes)], antes)

    def test_crear_el_csv_no_trunca_uno_que_aparece_a_la_vez(self):
        # Antes: _initialize_db comprobaba que faltaba y abría con 'w' (fuera del lock, desde una lectura):
        # si el operador restauraba el CSV justo entre las dos cosas, la lectura lo dejaba en solo la cabecera
        # (3 personas -> 0). Aquí se fuerza la ventana haciendo que la comprobación diga que falta.
        from unittest import mock
        antes = self._bytes()
        with mock.patch("src.database.os.path.exists", return_value=False):
            self.db._initialize_db()
        self.assertEqual(self._bytes(), antes)
        self.assertEqual(sorted(self.db.get_all()), ["alice", "bob", "carol"])

    def test_crear_el_csv_si_falta(self):
        os.remove(self.path)
        self.db._initialize_db()
        self.assertEqual(self._bytes().strip(), b"id,template")
        self.assertEqual(self.db.get_all(), {})

    def test_escribir_no_supera_los_topes_de_lectura(self):
        # Antes: _write_all_records no aplicaba MAX_DB_ROWS/MAX_DB_BYTES; una inscripción que los cruzaba
        # devolvía True y dejaba la BD ilegible (DatabaseReadError en todo, también al borrar)
        from unittest import mock
        import src.database as bd
        # filas: cabecera + 3 personas = 4 líneas; con tope 5 cabe una más y la siguiente no
        with mock.patch.object(bd, "MAX_DB_ROWS", 5):
            self.assertTrue(self.db.save_template("dave", self.nuevo))
            antes = self._bytes()
            with self.assertLogs("cjar", "WARNING") as log:
                self.assertFalse(self.db.save_template("erin", self.nuevo))
            self.assertIn("no se inscribe a «erin»", "\n".join(log.output))
            self.assertEqual(self._bytes(), antes)  # nada escrito
            self.assertEqual(sorted(self.db.get_all()), ["alice", "bob", "carol", "dave"])  # sigue legible
            self.assertTrue(self.db.delete_template("alice"))  # y se puede borrar
            self.assertTrue(self.db.save_template("erin", self.nuevo))  # y volver a inscribir
        # bytes: un tope que ya no deja sitio para otra plantilla (~5 KB)
        with mock.patch.object(bd, "MAX_DB_BYTES", len(self._bytes()) + 100):
            antes = self._bytes()
            self.assertFalse(self.db.save_template("frank", self.nuevo))
            self.assertEqual(self._bytes(), antes)
            self.assertIn("erin", self.db.get_all())

    def test_metodos_fallan_cerrados(self):
        from unittest import mock
        from src.database import DatabaseReadError
        sistema = BiometricSystem.__new__(BiometricSystem)  # sin cargar modelos
        from src.matcher import Matcher
        sistema.matcher, sistema.db = Matcher(), self.db
        with mock.patch.object(self.db, "_load", side_effect=DatabaseReadError("x")):
            self.assertFalse(sistema.inscribir_t("dave", self.nuevo))
            self.assertIsNone(sistema.identificar_t(self.nuevo))
        self.assertEqual(sorted(self.db.get_all()), ["alice", "bob", "carol"])


if __name__ == "__main__":
    unittest.main()