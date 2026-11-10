"""
Inscribe en la BD las personas del dataset del profesor (fotos con nombre
'nombre_apellido_AÑO.ext') y, opcionalmente, la persona extra elegida por el equipo.

Uso (dentro de Docker):
    docker compose run --rm cjar python cargar_dataset.py
    docker compose run --rm cjar python cargar_dataset.py --extra mi_persona fotos/extra/1.jpg fotos/extra/2.jpg
"""

import argparse
import re
from pathlib import Path

from src.biometric_system import BiometricSystem

EXTENSIONES = {".jpg", ".jpeg", ".png"}


def id_desde_fichero(ruta: Path) -> str:
    """'christian_bale_2012.jpg' -> 'christian_bale' (se quita el año final)."""
    return re.sub(r"_\d{4}$", "", ruta.stem)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dir", default="fotos/Dataset", help="Carpeta con las fotos del profesor")
    p.add_argument("--db", default="database/database.csv")
    p.add_argument("--extra", nargs="+", metavar=("ID", "FOTO"),
                   help="Persona extra del equipo: id seguido de una o más fotos")
    args = p.parse_args()

    sistema = BiometricSystem(db_path=args.db)
    fotos = sorted(f for f in Path(args.dir).iterdir() if f.suffix.lower() in EXTENSIONES)

    ok, fallos = 0, []
    for f in fotos:
        pid = id_desde_fichero(f)
        # Recargar el dataset actualiza a quien ya estuviera (lo decide el operador)
        if sistema.inscribir(pid, str(f), reemplazar=True):
            ok += 1
            print(f"[OK]    {pid}")
        else:
            fallos.append(pid)
            print(f"[FALLO] {pid} ({f.name}): no se detectó cara o imagen inválida")

    if args.extra:
        pid, *rutas = args.extra
        if rutas and sistema.inscribir(pid, rutas, reemplazar=True):
            ok += 1
            print(f"[OK]    {pid} (extra, {len(rutas)} fotos)")
        else:
            fallos.append(pid)
            print(f"[FALLO] {pid} (extra)")

    print(f"\nInscritas: {ok} | Fallos: {len(fallos)} {fallos if fallos else ''}")
    print(f"Total en la BD: {len(sistema.db.get_all())}")


if __name__ == "__main__":
    main()
