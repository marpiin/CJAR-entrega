"""
Descarga buffalo_s en el build y comprueba su SHA-256 ANTES de descomprimirlo: insightface
lo baja sin verificar nada. Solo se conservan los dos modelos que usa el sistema
(detección y reconocimiento); el resto del pack ni se extrae.

Los hashes se tomaron de la descarga del 2026-11-08 (confianza en el primer uso): si
deepinsight cambia el fichero, el build falla en vez de usar pesos distintos.
"""

import hashlib
import os
import sys
import urllib.request
import zipfile

URL = "https://github.com/deepinsight/insightface/releases/download/model-zoo/buffalo_s.zip"
ZIP_SHA256 = "d85a87f503f691807cd8bb97128bdf7a0660326cd9cd02657127fa978bab8b5e"
MODELOS = {
    "det_500m.onnx": "5e4447f50245bbd7966bd6c0fa52938c61474a04ec7def48753668a9d8b4ea3a",
    "w600k_mbf.onnx": "9cc6e4a75f0e2bf0b1aed94578f144d15175f357bdc05e815e5c4a02b319eb4f",
}
MAX_ZIP = 200 * 1024 * 1024


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for bloque in iter(lambda: f.read(1 << 20), b""):
            h.update(bloque)
    return h.hexdigest()


def main() -> None:
    destino = os.path.expanduser("~/.insightface/models/buffalo_s")
    os.makedirs(destino, exist_ok=True)
    zip_path = destino + ".zip"
    with urllib.request.urlopen(URL, timeout=120) as r, open(zip_path, "wb") as f:
        leidos = 0
        for bloque in iter(lambda: r.read(1 << 20), b""):
            leidos += len(bloque)
            if leidos > MAX_ZIP:
                sys.exit("buffalo_s.zip es más grande de lo esperado")
            f.write(bloque)
    if sha256(zip_path) != ZIP_SHA256:
        sys.exit("El SHA-256 de buffalo_s.zip no coincide: no se usa")
    with zipfile.ZipFile(zip_path) as zf:
        for nombre in MODELOS:
            # Se extrae por nombre exacto (sin rutas del zip) para evitar zip slip
            with zf.open(nombre) as src, open(os.path.join(destino, nombre), "wb") as dst:
                dst.write(src.read())
    os.remove(zip_path)
    for nombre, esperado in MODELOS.items():
        if sha256(os.path.join(destino, nombre)) != esperado:
            sys.exit(f"El SHA-256 de {nombre} no coincide")
    print("Modelos verificados:", ", ".join(MODELOS))


if __name__ == "__main__":
    main()
