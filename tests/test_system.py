"""
Tests del sistema: funcionamiento con templates sintéticos y los ataques del enunciado
(templates inválidos, CSV editado a mano, ids maliciosos, bombas de imagen...).
Usan una BD temporal, no la oficial.
"""

import os
import shutil
import tempfile
import unittest

os.environ["CJAR_CLAVE_BD"] = "ab" * 32  # clave de pruebas, nunca la real

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

    # 4. Trazabilidad: explicación por zonas de la cara
    def test_06_explicador_zonas(self):
        from src.explicador import Explicador, REGIONES
        cara = np.random.default_rng(6).integers(0, 256, (112, 112, 3), dtype=np.uint8)
        t = self.system.extractor.extract_many([cara])[0]  # ruido: extract() lo rechaza por calidad
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
            self.assertTrue(self.system.inscribir_t(uid, t, permitir_parecido=True))
            guardado = self.system.db.get_template(uid)
            self.assertIsNotNone(guardado, uid)
            self.assertAlmostEqual(float(np.linalg.norm(guardado)), 1.0, places=4)
            self.assertTrue(self.system.verificar_t(uid, t))
            self.assertTrue(self.system.db.delete_template(uid))  # las tres son la misma dirección
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
        t42 = self._generate_valid_template(4242)
        self.assertTrue(self.system.inscribir_t(42, t42))
        self.assertTrue(self.system.verificar_t("42", t42))

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


if __name__ == "__main__":
    unittest.main()
