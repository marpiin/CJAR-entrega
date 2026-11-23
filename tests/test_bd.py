"""
Tests de la base de datos cifrada y sellada: supuestos «el atacante lee la BD» y «el atacante escribe en la
BD», concurrencia, topes y herramienta de mantenimiento. No cargan los modelos. BD temporal, no la oficial.
"""

import csv
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

os.environ["CJAR_CLAVE_BD"] = "ab" * 32  # clave de pruebas, nunca la real

import numpy as np

from src.biometric_system import BiometricSystem
from src.cripto import ClaveError, Protector, cargar_clave
from src.database import (BiometricDatabase, DatabaseReadError, InscripcionRechazada, MAX_DB_ROWS, esqueleto)
from src.matcher import Matcher
from src.utils import parse_input_id


def _unit(seed):
    t = np.random.default_rng(seed).standard_normal(512)
    return (t / np.linalg.norm(t)).astype(np.float32)


def _sistema(db):
    """BiometricSystem sobre una BD concreta, sin cargar los modelos (para los métodos _t)."""
    s = BiometricSystem.__new__(BiometricSystem)
    s.db, s.matcher = db, Matcher()
    return s


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.path = os.path.join(self.dir, "db.csv")
        self.db = BiometricDatabase(self.path)
        self.personas = {uid: _unit(i) for i, uid in enumerate(("alice", "bob", "carol"), 1)}
        for uid, t in self.personas.items():
            self.assertTrue(self.db.save_template(uid, t))

    def _bytes(self):
        with open(self.path, "rb") as f:
            return f.read()

    def _escribir(self, datos):
        with open(self.path, "wb") as f:
            f.write(datos)

    def assertRechazada(self, msg=None):
        db = BiometricDatabase(self.path, crear=False)
        with self.assertRaises(DatabaseReadError, msg=msg):
            db.get_all()
        s = _sistema(db)
        for uid, t in self.personas.items():
            self.assertFalse(s.verificar_t(uid, t), msg)
            self.assertIsNone(s.identificar_t(t), msg)
        antes = self._bytes() if os.path.exists(self.path) else None
        self.assertFalse(s.inscribir_t("nuevo", _unit(77)), msg)
        self.assertEqual(self._bytes() if os.path.exists(self.path) else None, antes, msg)  # no se escribe encima


class TestLeerLaBD(Base):
    """Supuesto: el atacante consigue leer database.csv (sin la clave)."""

    def test_no_hay_plantillas_en_claro(self):
        texto = self._bytes().decode("utf-8")
        for t in self.personas.values():
            self.assertNotIn(f"{t[0]:.5f}", texto)
            self.assertNotIn(t.tobytes()[:16], self._bytes())

    def test_la_celda_robada_no_sirve_como_plantilla(self):
        s = _sistema(self.db)
        for fila in csv.reader(self._bytes().decode("utf-8").splitlines()[1:-1]):
            self.assertFalse(s.verificar_t(fila[0], fila[1]))
            self.assertIsNone(s.identificar_t(fila[1]))

    def test_la_misma_plantilla_cifra_distinto_cada_vez(self):
        p = self.db.protector  # no vinculabilidad: dos filas iguales no se reconocen comparando el fichero
        self.assertNotEqual(p.cifrar("a", _unit(1)), p.cifrar("a", _unit(1)))

    def test_plantilla_de_otra_instalacion_no_sirve(self):
        # Cancelable: la misma cara con otra clave da una plantilla sin relación (y el coseno propio se conserva)
        cara1, cara2 = _unit(50), _unit(51)
        p, otra = self.db.protector, Protector(b"\x01" * 32)
        a, b = p.proteger(cara1), p.proteger(cara2)
        self.assertAlmostEqual(float(a @ b), float(cara1 @ cara2), places=5)
        np.testing.assert_allclose(p.desproteger(a), cara1, atol=1e-6)
        self.assertLess(abs(float(a @ otra.proteger(cara1))), 0.25)
        self.assertLess(abs(float(a @ cara1)), 0.25)

    def test_clave_obligatoria_y_valida(self):
        entorno = {k: v for k, v in os.environ.items() if k != "CJAR_CLAVE_BD"}
        with mock.patch.dict(os.environ, {**entorno, "CJAR_CLAVE_BD_FICHERO": os.path.join(self.dir, "no")}, clear=True):
            with self.assertRaises(ClaveError):
                cargar_clave()
        for mala in ("corta", "zz" * 32, "ab" * 31):
            with mock.patch.dict(os.environ, {"CJAR_CLAVE_BD": mala}):
                with self.assertRaises(ClaveError):
                    cargar_clave()
        with self.assertRaises(ClaveError):
            Protector(b"corta")


class TestEscribirEnLaBD(Base):
    """Supuesto: el atacante consigue escribir en database.csv (sin la clave). Todo cambio se rechaza."""

    def test_cualquier_edicion_se_rechaza(self):
        original = self._bytes()
        L = original.splitlines()
        j = lambda filas: b"\r\n".join(filas) + b"\r\n"
        claro = b";".join(b"%.7f" % v for v in _unit(9))
        celda = lambda fila: fila.split(b",", 1)[1]
        ajeno = Protector(b"\x02" * 32)
        casos = {
            "fila en claro añadida": j(L[:-1] + [b"intruso," + claro] + L[-1:]),
            "fila cifrada con otra clave": j(L[:-1] + [b"intruso," + ajeno.cifrar("intruso", _unit(9)).encode()] + L[-1:]),
            "plantilla sustituida": j([L[0], b"alice," + claro] + L[2:]),
            "celdas intercambiadas": j([L[0], b"alice," + celda(L[2]), b"bob," + celda(L[1])] + L[3:]),
            "fila duplicada": j(L[:2] + L[1:]),
            "fila borrada": j(L[:1] + L[2:]),
            "filas reordenadas": j([L[0], L[2], L[1]] + L[3:]),
            "id cambiado": original.replace(b"alice,", b"alicf,"),
            "truncada": original[: len(original) // 2],
            "sin sello": j(L[:-1]),
            "sello dos veces": j(L + L[-1:]),
            "sello de otra clave": j(L[:-1] + [ajeno.sellar(L[:-1], 9)]),
            "contador del sello cambiado": original.replace(b"cjar1:3:", b"cjar1:9:"),
            "sello mal formado": j(L[:-1] + [b"__sello__,cjar1:x:y"]),
            "sin cabecera": j(L[1:]),
            "basura al final": original + b"basura\r\n",
            "bytes no UTF-8": original[:20] + b"\xff" + original[21:],
            "BOM al principio": b"\xef\xbb\xbf" + original,
            "vacía": b"",
            "solo cabecera": b"id,template\r\n",
            "campo enorme": j(L[:-1] + [b"z," + b"x" * 200_000] + L[-1:]),
        }
        for nombre, datos in casos.items():
            self._escribir(datos)
            self.assertRechazada(nombre)
        for k in range(0, len(original), 97):  # un bit en cualquier sitio (los finales de línea no cuentan)
            if original[k] in b"\r\n":
                continue
            self._escribir(original[:k] + bytes([original[k] ^ 1]) + original[k + 1:])
            with self.assertRaises(DatabaseReadError, msg=k):
                BiometricDatabase(self.path, crear=False).get_all()
        self._escribir(original)
        self.assertEqual(sorted(self.db.get_all()), sorted(self.personas))

    def test_fila_movida_no_descifra_aunque_el_sello_valga(self):
        # Segunda barrera: el id va autenticado con la plantilla
        p = self.db.protector
        filas = [b"id,template", b"bob," + p.cifrar("alice", _unit(1)).encode()]
        self._escribir(b"\n".join(filas + [p.sellar(filas, 99)]))
        self.assertRechazada("fila movida")

    def test_borrar_el_fichero_no_es_una_bd_vacia(self):
        # Antes: un CSV borrado se volvía a crear vacío y cualquiera podía inscribirse con el id de otro
        os.unlink(self.path)
        with self.assertRaises(DatabaseReadError):
            self.db.get_all()
        with self.assertRaises(DatabaseReadError):
            self.db.save_template("alice", _unit(9))
        self.assertFalse(os.path.exists(self.path))
        self.assertRechazada("borrada")

    def test_copia_antigua_en_caliente_se_rechaza(self):
        vieja = self._bytes()
        self.assertTrue(self.db.delete_template("bob"))
        self._escribir(vieja)  # recupera a una persona dada de baja
        with self.assertRaises(DatabaseReadError):
            self.db.get_all()

    def test_finales_de_linea_de_git_no_rompen_el_sello(self):
        self._escribir(self._bytes().replace(b"\r\n", b"\n"))
        self.assertEqual(sorted(BiometricDatabase(self.path).get_all()), sorted(self.personas))

    def test_aviso_una_vez_por_version_del_fichero(self):
        self._escribir(self._bytes() + b"x\r\n")
        with self.assertLogs("cjar", "WARNING") as cm:
            for _ in range(5):
                with self.assertRaises(DatabaseReadError):
                    self.db.get_all()
        self.assertEqual(len(cm.records), 1)
        self.assertIn("RECHAZADA", cm.output[0])

    def test_lectura_fallida_no_reescribe(self):
        import builtins
        antes, real = self._bytes(), builtins.open

        def bloqueado(path, mode="r", *a, **k):
            if str(path) == self.path and "r" in mode:
                raise PermissionError("bloqueado por otro programa")
            return real(path, mode, *a, **k)

        otra = BiometricDatabase(self.path)
        with mock.patch("builtins.open", bloqueado):
            with self.assertRaises(DatabaseReadError):
                otra.save_template("dave", _unit(4))
        self.assertEqual(self._bytes(), antes)


class TestFuncionamiento(Base):
    def test_guardar_leer_borrar(self):
        for uid, t in self.personas.items():
            np.testing.assert_allclose(self.db.get_template(uid), t, atol=1e-6)
        self.assertFalse(self.db.save_template("alice", _unit(9)))
        self.assertTrue(self.db.save_template("alice", _unit(9), reemplazar=True))
        self.assertTrue(self.db.delete_template("bob"))
        self.assertFalse(self.db.delete_template("bob"))
        self.assertEqual(sorted(BiometricDatabase(self.path).get_all()), ["alice", "carol"])
        self.assertIsNone(self.db.get_template("nadie"))
        self.assertIsNone(self.db.get_template("=mal"))

    def test_ids_con_comas_comillas_y_tildes(self):
        for k, uid in enumerate(['garcía, ana', 'o"neil', "muñoz", "张伟"], 20):
            self.assertTrue(self.db.save_template(uid, _unit(k)))
        self.assertEqual(len(BiometricDatabase(self.path).get_all()), 7)

    def test_ids_reservados_o_raros(self):
        for mal in ("__sello__", "__SELLO__", "~x", "a/b", "..", "a b", "-x", "=x", "a\nb"):
            with self.assertRaises(ValueError, msg=ascii(mal)):
                parse_input_id(mal)

    def test_lo_devuelto_no_corrompe_la_cache(self):
        copia = self.db.get_all()
        with self.assertRaises(ValueError):
            copia["alice"][0] = 9.0  # solo lectura
        copia.clear()
        self.assertEqual(len(self.db.get_all()), 3)

    def test_no_relee_mientras_no_cambie(self):
        with mock.patch.object(self.db, "_leer", wraps=self.db._leer) as leer:
            for _ in range(3):
                self.db.get_all()
                self.db.get_template("alice")
            self.assertLessEqual(leer.call_count, 1)

    @unittest.skipUnless(os.name == "posix", "el lock entre instancias es flock")
    def test_escritores_concurrentes_no_pierden_registros(self):
        otra = BiometricDatabase(self.path)
        barrera = threading.Barrier(2)

        def escribir(db, prefijo):
            barrera.wait()
            for i in range(15):
                db.save_template(f"{prefijo}{i}", _unit(100 + i))

        hilos = [threading.Thread(target=escribir, args=(d, p)) for d, p in ((self.db, "a"), (otra, "b"))]
        [h.start() for h in hilos]
        [h.join() for h in hilos]
        self.assertEqual(len(BiometricDatabase(self.path).get_all()), 33)

    @unittest.skipUnless(hasattr(os, "symlink") and os.name == "posix", "necesita enlaces simbólicos POSIX")
    def test_escribir_no_sigue_enlaces(self):
        # Si la ruta de la BD es un enlace, se sustituye el enlace: no se escribe en el fichero al que apunta
        real = os.path.join(self.dir, "real.csv")
        os.rename(self.path, real)
        os.symlink(real, self.path)
        with open(real, "rb") as f:
            antes = f.read()
        self.assertTrue(self.db.save_template("dave", _unit(4)))
        with open(real, "rb") as f:
            self.assertEqual(f.read(), antes)
        self.assertFalse(os.path.islink(self.path))
        self.assertEqual([n for n in os.listdir(self.dir) if n.endswith(".tmp")], [])

    def test_lectores_concurrentes_con_un_escritor(self):
        # Antes: un lector que abría la versión N mientras se escribía la N+1 la daba por «copia antigua»
        fin, errores = threading.Event(), []

        def leer():
            while not fin.is_set():
                try:
                    self.db.existe("alice")
                except Exception as exc:
                    errores.append(exc)

        hilos = [threading.Thread(target=leer, daemon=True) for _ in range(4)]
        [h.start() for h in hilos]
        try:
            for k in range(25):
                self.assertTrue(self.db.save_template("z", _unit(300 + k), reemplazar=True))
        finally:
            fin.set()
            [h.join() for h in hilos]
        self.assertEqual(errores, [])

    def test_crear_no_pisa_un_fichero_existente(self):
        antes = self._bytes()
        self.db._initialize_db()
        self.assertEqual(self._bytes(), antes)

    def test_sin_crear_no_crea(self):
        ruta = os.path.join(self.dir, "otra.csv")
        with self.assertRaises(DatabaseReadError):
            BiometricDatabase(ruta, crear=False).get_all()
        self.assertFalse(os.path.exists(ruta))

    def test_topes(self):
        import src.database as bd
        with mock.patch.object(bd, "MAX_DB_ROWS", 6):  # cabecera + 3 + sello = 5
            self.assertTrue(self.db.save_template("dave", _unit(4)))
            antes = self._bytes()
            with self.assertLogs("cjar", "WARNING"):
                self.assertFalse(self.db.save_template("erin", _unit(5)))
            self.assertEqual(self._bytes(), antes)
            self.assertTrue(self.db.delete_template("alice"))
        with mock.patch.object(bd, "MAX_DB_BYTES", len(self._bytes()) + 100):
            self.assertFalse(self.db.save_template("frank", _unit(6)))
        self._escribir(self._bytes() + b"a,\r\n" * (MAX_DB_ROWS + 1))
        with self.assertRaises(DatabaseReadError):
            self.db.get_all()

    def test_herramienta_de_mantenimiento(self):
        raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if not os.path.exists(os.path.join(raiz, "admin_bd.py")):
            self.skipTest("admin_bd.py no está")

        def orden(*args, db=self.path, **env):
            return subprocess.run([sys.executable, os.path.join(raiz, "admin_bd.py"), "--db", db, *args], cwd=raiz,
                                  capture_output=True, text=True, timeout=120, env={**os.environ, **env})

        claro, copia, clave = (os.path.join(self.dir, n) for n in ("claro.csv", "copia.csv", "nueva.key"))
        self.assertIn("3 personas", orden("verificar").stdout)
        self.assertEqual(orden("exportar", claro).returncode, 0)
        self.assertEqual(orden("exportar", claro).returncode, 1)  # no pisa ficheros
        self.assertEqual(orden("importar", claro, db=copia).returncode, 0)
        otra = BiometricDatabase(copia).get_all()
        for uid, t in self.personas.items():
            np.testing.assert_allclose(otra[uid], t, atol=1e-5)
        with open(claro, "a", encoding="utf-8") as f:
            f.write("-malo," + ";".join(["0.1"] * 512) + "\n")
        incompleta = os.path.join(self.dir, "incompleta.csv")
        self.assertEqual(orden("importar", claro, db=incompleta).returncode, 1)
        self.assertFalse(os.path.exists(incompleta))  # todo o nada
        self.db.save_contraste("alice", _unit(60))
        self.assertEqual(orden("rotar", clave).returncode, 0)
        with open(clave) as f:
            nueva = BiometricDatabase(self.path + ".rotada", clave=bytes.fromhex(f.read().strip()))
        self.assertEqual(sorted(nueva.get_all()), sorted(self.personas))
        self.assertEqual(len(nueva.get_contrastes()), 1)
        self.assertLess(abs(float(nueva.get_all()["alice"] @ self.personas["alice"])), 0.25)  # la vieja ya no sirve
        self.assertEqual(orden("verificar", CJAR_CLAVE_BD="cd" * 32).returncode, 1)


class TestReglasDeInscripcionYDecision(Base):
    """Métodos _t: alias, plantillas lobo, identidades compartidas y gemelos."""

    def setUp(self):
        super().setUp()
        self.s = _sistema(self.db)

    def _parecida(self, t, cos, seed=500):
        r = _unit(seed)
        r = r - (r @ t) * t
        return (cos * t + np.sqrt(1 - cos * cos) * r / np.linalg.norm(r)).astype(np.float32)

    def test_misma_cara_con_otro_id_se_rechaza(self):
        # Antes: inscribir_t('atacante', plantilla_de_alice) devolvía True
        for cos in (1.0, 0.6, 0.45):
            self.assertFalse(self.s.inscribir_t("atacante", self._parecida(self.personas["alice"], cos)))
        self.assertTrue(self.s.inscribir_t("parecida_lejana", self._parecida(self.personas["alice"], 0.3)))

    def test_ids_que_se_confunden(self):
        for k, alias in enumerate(("Alice", "alice.", "a lice", "аlice", "ａlice", "álice", "ALICE"), 600):
            self.assertFalse(self.s.inscribir_t(alias, _unit(k)), ascii(alias))
        for k, alias in enumerate(("\u0251lice", "\u1d00lice", "alic\u0585", "\u02bcalice", "a1ice", "al1ce", "4lice", "a\u01c0ice"), 620):
            self.assertFalse(self.s.inscribir_t(alias, _unit(k)), ascii(alias))
        for k, bueno in enumerate(("\u5f20\u4f1f", "\u0418\u0432\u0430\u043d", "Nguy\u1ec5n", "Jos\u00e9 3", "hwang_min-hyun", "1234",
                                       "\u0915", "\u0915\u093f", "\u0915\u0941", "Juan5", "Juans"), 640):
            self.assertTrue(self.s.inscribir_t(bueno, _unit(k)), ascii(bueno))
        self.assertNotEqual(esqueleto("alice"), esqueleto("alicia"))
        self.assertNotEqual(esqueleto("user_1"), esqueleto("user_2"))
        self.assertTrue(self.s.inscribir_t("alicia", _unit(700)))

    def test_permitir_parecido_es_explicito(self):
        self.assertTrue(self.s.inscribir_t("gemela", self._parecida(self.personas["alice"], 0.6), permitir_parecido=True))

    def test_plantilla_lobo(self):
        # Antes: la media de 4-5 plantillas verificaba como las 4-5 personas a la vez
        lobo = sum(self.personas.values())
        lobo /= np.linalg.norm(lobo)
        self.assertGreaterEqual(min(float(lobo @ t) for t in self.personas.values()), 0.48)
        self.assertLessEqual(sum(self.s.verificar_t(u, lobo) for u in self.personas), 1)
        self.assertFalse(self.s.inscribir_t("lobo", lobo))
        par = self.personas["alice"] + self.personas["bob"]
        par /= np.linalg.norm(par)
        for u in ("alice", "bob"):
            self.assertFalse(self.s.verificar_t(u, par))
        self.assertIsNone(self.s.identificar_t(par))

    def test_identidad_compartida_por_dos_personas(self):
        # Antes: la media de las fotos de dos personas se inscribía y las dos verificaban como ese id
        with self.assertRaises(ValueError):
            self.s.guardar("mezcla", [_unit(801), _unit(802)], permitir_parecido=True)
        self.assertTrue(self.s.guardar("una", [_unit(803), self._parecida(_unit(803), 0.7)]))

    def test_gemelos_inscritos_los_dos(self):
        a = self.personas["alice"]
        gemela = self._parecida(a, 0.6)
        self.assertTrue(self.s.inscribir_t("gemela", gemela, permitir_parecido=True))
        self.assertFalse(self.s.verificar_t("alice", gemela))
        self.assertTrue(self.s.verificar_t("alice", a))
        medio = a + gemela
        medio /= np.linalg.norm(medio)
        self.assertIsNone(self.s.identificar_t(medio))  # empate: no se decide

    def test_contraste_de_un_gemelo(self):
        a = self.personas["alice"]
        gemelo = self._parecida(a, 0.52)
        self.assertTrue(self.s.verificar_t("alice", gemelo))  # sin contraste lo acepta: limitación conocida
        self.assertTrue(self.db.save_contraste("alice", gemelo))
        self.assertFalse(self.s.verificar_t("alice", gemelo))
        self.assertIsNone(self.s.identificar_t(gemelo))
        self.assertTrue(self.s.verificar_t("alice", a))
        self.assertEqual(len(self.db.get_all()), 3)  # no es una identidad
        self.assertIsNone(self.db.get_template("~alice~1"))
        self.assertFalse(self.s.verificar_t("~alice~1", gemelo))
        self.assertFalse(self.s.inscribir_t("gemelo", gemelo))  # ni se inscribe sin permiso
        self.assertTrue(self.s.inscribir_t("alice", a, reemplazar=True))  # actualizarla no choca con su contraste
        self.assertEqual(self.db.get_contrastes(), {})  # que se quita: era de la plantilla anterior
        self.assertTrue(self.db.save_contraste("alice", gemelo))
        with self.assertRaises(InscripcionRechazada):
            self.db.save_contraste("bob", self.personas["bob"])  # la propia persona: la dejaría sin acceso
        self.assertFalse(self.db.save_contraste("nadie", _unit(1)))
        self.assertTrue(self.db.delete_contrastes("alice"))
        self.assertTrue(self.s.verificar_t("alice", gemelo))
        self.db.save_contraste("alice", gemelo)
        self.db.delete_template("alice")
        self.assertEqual(self.db.get_contrastes(), {})

    def test_umbral_y_margen_invalidos(self):
        for th in (-1, 0, 1.5, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                Matcher(threshold=th)
        with self.assertRaises(ValueError):
            Matcher(margen=float("nan"))

    def test_metodos_fallan_cerrados(self):
        with mock.patch.object(self.db, "_load", side_effect=DatabaseReadError("x")), \
                mock.patch.object(self.db, "_plantillas", side_effect=DatabaseReadError("x")):
            self.assertIs(self.s.inscribir_t("dave", _unit(4)), False)
            self.assertIs(self.s.verificar_t("alice", self.personas["alice"]), False)
            self.assertIsNone(self.s.identificar_t(self.personas["alice"]))


if __name__ == "__main__":
    unittest.main()
