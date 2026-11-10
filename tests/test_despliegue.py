"""
Tests de la configuración de despliegue (Dockerfile, docker-compose.yml, dependencias):
que no se vuelva a correr como root, a usar dependencias o imagen base sin fijar, a
descargar modelos sin comprobarlos ni a quitar los límites del contenedor.
No ejecutan Docker: leen los ficheros del repositorio.
"""

import re
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent


def reglas_de(nombre: str) -> list:
    """Líneas de un .gitignore/.dockerignore sin comentarios ni líneas vacías."""
    return [l.strip() for l in leer(nombre).splitlines() if l.strip() and not l.strip().startswith("#")]


def leer(nombre: str) -> str:
    return (RAIZ / nombre).read_text(encoding="utf-8")


class TestDespliegue(unittest.TestCase):
    def test_imagen_base_fijada_por_digest(self):
        froms = re.findall(r"^FROM\s+(\S+)", leer("Dockerfile"), re.M)
        self.assertTrue(froms)
        for imagen in froms:
            self.assertRegex(imagen, r"@sha256:[0-9a-f]{64}$")

    def test_no_corre_como_root(self):
        usuarios = re.findall(r"^USER\s+(\S+)", leer("Dockerfile"), re.M)
        self.assertTrue(usuarios, "el Dockerfile no fija USER")
        self.assertNotIn(usuarios[-1], ("root", "0"))

    def test_dependencias_fijadas(self):
        for fichero in ("requirements.txt", "requirements.lock", "requirements-dev.txt"):
            for linea in leer(fichero).splitlines():
                linea = linea.split("#")[0].strip()
                if linea and not linea.startswith("-c "):
                    self.assertRegex(linea, r"^[A-Za-z0-9_.\-\[\]]+==[0-9][\w.]*$", f"{fichero}: {linea}")
        self.assertIn("-c requirements.lock", leer("Dockerfile"))

    def test_modelos_verificados(self):
        script = leer("docker/descargar_modelos.py")
        self.assertIn("descargar_modelos.py", leer("Dockerfile"))
        self.assertNotIn("FaceAnalysis", leer("Dockerfile"))  # nada de descargas sin verificar
        self.assertEqual(len(re.findall(r'"[0-9a-f]{64}"', script)), 3)

    def test_usuario_del_contenedor_configurable(self):
        # Antes: el usuario era siempre el 1000 de la imagen; con database/ de otro uid no se podía
        # inscribir. Probado de verdad con CJAR_UID=2000 (raíz de solo lectura, modelos cargados)
        compose = leer("docker-compose.yml")
        self.assertIn('user: "${CJAR_UID:-1000}:${CJAR_GID:-1000}"', compose)  # por defecto sigue sin ser root
        # /home/cjar nace con modo 0700: sin esto otro uid no llega a los modelos y InsightFace intenta descargarlos
        self.assertIn("chmod 755 /home/cjar", leer("Dockerfile"))

    def test_env_ignorado(self):
        # Antes: .env no estaba en .gitignore ni en .dockerignore (el README recomienda crear uno,
        # y compose lee de él CJAR_ADMIN_TOKEN): un `git add -A` lo subía y `COPY . .` lo metía en la imagen
        for fichero in (".gitignore", ".dockerignore"):
            reglas = reglas_de(fichero)
            for regla in (".env", ".env.*"):
                self.assertIn(regla, reglas, f"{fichero} no ignora {regla}")
        self.assertIn("!.env.example", reglas_de(".gitignore"))  # una plantilla sin claves sí puede versionarse

    def test_la_imagen_solo_copia_lo_necesario(self):
        # Antes: `COPY . .` copiaba toda la carpeta del proyecto (con lo que hubiera: .env, claves...)
        dockerfile = leer("Dockerfile")
        self.assertNotRegex(dockerfile, r"(?m)^COPY\s+\.\s+\.\s*$")
        permitido = {"requirements.txt", "requirements.lock", "requirements-dev.txt", "docker/descargar_modelos.py",
                     "src", "web", "tests", "docker", "main.py", "cargar_dataset.py", "Dockerfile",
                     "docker-compose.yml", ".gitignore", ".dockerignore"}
        origenes = []
        for linea in re.findall(r"(?m)^COPY\s+(.+)$", dockerfile):
            origenes += linea.split()[:-1]  # el último elemento es el destino
        self.assertTrue(origenes)
        for origen in origenes:
            self.assertIn(origen, permitido, f"COPY de algo no previsto: {origen}")
        for prohibido in (".env", "fotos", "database"):
            self.assertNotIn(prohibido, origenes)

    def test_rotacion_de_logs(self):
        # Antes: el log del contenedor (json-file) crecía sin límite
        compose = leer("docker-compose.yml")
        self.assertIn("logging:", compose)
        self.assertIn('max-size: "10m"', compose)
        self.assertIn('max-file: "3"', compose)

    def test_contenedor_acotado(self):
        compose = leer("docker-compose.yml")
        for clave in ("read_only: true", "cap_drop: [ALL]", "no-new-privileges:true",
                      "mem_limit:", "pids_limit:", "cpus:"):
            self.assertIn(clave, compose)
        self.assertIn("127.0.0.1:8765:8000", compose)
        for codigo in ("./src:/app/src", "./web:/app/web", "./main.py:/app/main.py"):
            self.assertIn(codigo + ":ro", compose)
        self.assertIn("--limit-concurrency", leer("Dockerfile"))

    def test_limite_de_conexiones_no_se_agota_con_un_ataque_trivial(self):
        # Antes: --limit-concurrency 32. uvicorn lo cuenta contra las conexiones ABIERTAS, también las que no
        # envían nada: con 31-32 sockets sin clave toda la web (administrador incluido) respondía 503. Medido
        # en el sandbox: con 1024, 1000 sockets no hacen daño (con 1100 sí: no es una solución, solo sube el coste).
        m = re.search(r'"--limit-concurrency",\s*"(\d+)"', leer("Dockerfile"))
        self.assertIsNotNone(m)
        self.assertGreaterEqual(int(m.group(1)), 512)


if __name__ == "__main__":
    unittest.main()
