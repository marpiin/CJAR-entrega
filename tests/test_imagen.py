"""
Tests de las defensas sobre la imagen: tipos de píxel, calidad de cara, perturbaciones y fotos de grupo.
Cargan los modelos, pero no necesitan fotos reales. BD temporal, no la oficial.
"""

import os
import shutil
import tempfile
import unittest
from unittest import mock

os.environ["CJAR_CLAVE_BD"] = "ab" * 32  # clave de pruebas, nunca la real

import numpy as np

from src.biometric_system import MAX_FOTOS_INSCRIPCION, BiometricSystem
from src.utils import parse_input_image


class TestTiposDePixel(unittest.TestCase):
    """Antes un array float en 0-1 quedaba negro y uno uint16 blanco: todas las caras daban la misma plantilla."""

    def setUp(self):
        self.img = np.random.default_rng(0).integers(0, 256, (112, 112, 3), dtype=np.uint8)
        self.img[0, 0] = (0, 128, 255)

    def test_escalas_reconocibles(self):
        for nombre, v in {"float32 0-1": self.img.astype(np.float32) / 255, "float64 0-1": self.img / 255.0,
                          "uint16": self.img.astype(np.uint16) * 257, "float 0-255": self.img.astype(np.float32),
                          "int64 0-255": self.img.astype(np.int64)}.items():
            np.testing.assert_array_equal(parse_input_image(v), self.img, nombre)

    def test_escalas_ambiguas_se_rechazan(self):
        for nombre, v in {"-1..1": self.img.astype(np.float32) / 127.5 - 1, "negativos": self.img.astype(np.int16) - 10,
                          "> 255": self.img.astype(np.int32) + 1, "bool": self.img > 127, "complejo": self.img * 1j,
                          "objeto": self.img.astype(object), "float > 255": self.img.astype(np.float32) * 2,
                          "nan": np.full((9, 9, 3), np.nan)}.items():
            with self.assertRaises(ValueError, msg=nombre):
                parse_input_image(v)

    def test_cuatro_canales_mantienen_el_orden_bgr(self):
        bgra = np.dstack([self.img, np.full((112, 112), 255, np.uint8)])
        np.testing.assert_array_equal(parse_input_image(bgra), self.img)

    def test_transparencia_y_animaciones(self):
        # Antes: un PNG invisible (alfa 0) verificaba, y de un GIF solo contaba el primer fotograma
        import io
        from PIL import Image

        def fichero(img, formato, **k):
            b = io.BytesIO()
            img.save(b, formato, **k)
            return b.getvalue()

        rgb = Image.fromarray(self.img)
        opaca, invisible = rgb.convert("RGBA"), rgb.convert("RGBA")
        invisible.putalpha(0)
        self.assertEqual(parse_input_image(fichero(opaca, "PNG")).shape, (112, 112, 3))
        otra = Image.fromarray(255 - self.img)
        for nombre, datos in {"alfa 0": fichero(invisible, "PNG"), "webp alfa": fichero(invisible, "WEBP"),
                              "gif": fichero(rgb, "GIF", save_all=True, append_images=[otra]),
                              "webp": fichero(rgb, "WEBP", save_all=True, append_images=[otra]),
                              "apng": fichero(rgb, "PNG", save_all=True, append_images=[otra]),
                              "tiff": fichero(rgb, "TIFF", save_all=True, append_images=[otra])}.items():
            with self.assertRaises(ValueError, msg=nombre):
                parse_input_image(datos)
        self.assertEqual(parse_input_image(fichero(rgb, "GIF")).shape, (112, 112, 3))

    def test_rutas_de_red(self):
        for ruta in (r"\\servidor\recurso\a.jpg", "//servidor/recurso/a.jpg"):
            with mock.patch("os.path.isfile", side_effect=AssertionError("no debe tocar la red")):
                with self.assertRaises(ValueError):
                    parse_input_image(ruta)


class TestDefensasConModelos(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.s = BiometricSystem(db_path=os.path.join(cls.tmp, "db.csv"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_sd_rechaza_lo_que_no_es_una_cara(self):
        # Antes: inscribir_sd aceptaba una imagen lisa, ruido o un píxel, y cualquier otra imagen lisa verificaba
        rng = np.random.default_rng(1)
        for nombre, im in {"negra": np.zeros((112, 112, 3), np.uint8), "blanca": np.full((112, 112, 3), 255, np.uint8),
                           "ruido": rng.integers(0, 256, (112, 112, 3), dtype=np.uint8),
                           "1x1": np.zeros((1, 1, 3), np.uint8)}.items():
            self.assertFalse(self.s.inscribir_sd("cosa", im), nombre)
            self.assertIsNone(self.s.identificar_sd(im), nombre)

    def test_perturbacion_inestable_se_rechaza(self):
        # Se simula una cara cuyo parecido desaparece al suavizarla
        e = self.s.extractor
        ref, otra = np.eye(512, dtype=np.float32)[0], np.eye(512, dtype=np.float32)[1]
        cara = np.zeros((112, 112, 3), np.uint8)
        with mock.patch.object(e, "extract_many", return_value=np.stack([otra, otra])):
            self.assertFalse(e.estable(cara, ref, ref))
        with mock.patch.object(e, "extract_many", return_value=np.stack([ref, ref])):
            self.assertTrue(e.estable(cara, ref, ref))
        with mock.patch.object(e, "estable", return_value=False):
            self.s.db.save_template("victima", ref)
            with self.assertLogs("cjar", "WARNING") as cm:
                self.assertFalse(self.s.decidir("victima", ref, cara))
                self.assertIsNone(self.s.buscar(ref, cara))
            self.assertIn("adversaria", cm.output[0])
        self.assertTrue(self.s.verificar_t("victima", ref))

    def test_la_estabilidad_se_calcula_siempre(self):
        # Antes solo si había coincidencia: el tiempo de respuesta delataba que se estaba cerca del umbral
        ref, cara = np.eye(512, dtype=np.float32)[2], np.zeros((112, 112, 3), np.uint8)
        with mock.patch.object(self.s.extractor, "estable", return_value=True) as estable:
            self.s.decidir("no_existe", ref, cara)
            self.s.buscar(ref, cara)
            self.assertEqual(estable.call_count, 2)

    def test_cara_pequena_al_inscribir(self):
        class Cara:
            bbox = np.array([0, 0, 40, 40], dtype=np.float32)
            kps = np.array([[38, 51], [73, 51], [56, 71], [41, 92], [70, 92]], dtype=np.float32)

        with mock.patch.object(self.s.detector.app, "get", return_value=[Cara()]):
            with self.assertRaises(ValueError):
                self.s.detector.detect_and_align(np.zeros((200, 200, 3), np.uint8), unica=True)
            self.assertEqual(self.s.detector.detect_and_align(np.zeros((200, 200, 3), np.uint8)).shape, (112, 112, 3))

    def test_demasiadas_fotos(self):
        with mock.patch.object(self.s.extractor, "extract") as extract:
            self.assertFalse(self.s.inscribir_sd("x", [np.zeros((112, 112, 3), np.uint8)] * (MAX_FOTOS_INSCRIPCION + 1)))
            self.assertFalse(self.s.inscribir_sd("x", []))
            extract.assert_not_called()  # ni se empiezan a procesar

    def test_foto_con_dos_caras_al_inscribir(self):
        class Cara:
            def __init__(self, lado):
                self.bbox = np.array([0, 0, lado, lado], dtype=np.float32)
                self.kps = np.array([[38, 51], [73, 51], [56, 71], [41, 92], [70, 92]], dtype=np.float32)

        img = np.zeros((200, 200, 3), np.uint8)
        for lado, ambigua in ((90, True), (40, False)):
            with mock.patch.object(self.s.detector.app, "get", return_value=[Cara(lado), Cara(100)]):
                if ambigua:
                    with self.assertRaises(ValueError):
                        self.s.detector.detect_and_align(img, unica=True)
                else:
                    self.assertEqual(self.s.detector.detect_and_align(img, unica=True).shape, (112, 112, 3))
                self.assertEqual(self.s.detector.detect_and_align(img).shape, (112, 112, 3))


if __name__ == "__main__":
    unittest.main()
