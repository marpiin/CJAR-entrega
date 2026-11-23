"""
Mantenimiento de la base de datos cifrada (solo el operador: hace falta la clave).

    python admin_bd.py verificar                       ¿está íntegra? ¿cuántas personas hay?
    python admin_bd.py importar plantillas.csv         crea la BD cifrada desde plantillas de ArcFace en claro (id,template)
    python admin_bd.py exportar copia.csv              saca las plantillas de ArcFace en claro (datos biométricos)
    python admin_bd.py rotar clave_nueva.key           genera otra clave y vuelve a cifrar la BD con ella
    python admin_bd.py quitar-contrastes ID            borra los contrastes (gemelos) de una persona
"""

import argparse
import csv
import os
import sys

from src.cripto import ClaveError, nueva_clave
from src.database import BiometricDatabase, DatabaseReadError
from src.utils import parse_input_id, parse_input_template


def _crear(ruta: str, modo: int = 0o600):
    """Abre el fichero en exclusiva (no pisa nada) y, si el sistema lo permite, solo legible por su dueño."""
    fd = os.open(ruta, os.O_WRONLY | os.O_CREAT | os.O_EXCL, modo)
    return os.fdopen(fd, "w", encoding="utf-8", newline="")


def importar(ruta_db: str, origen: str) -> None:
    if os.path.exists(ruta_db):
        raise ValueError(f"'{ruta_db}' ya existe: muévelo antes de importar.")
    csv.field_size_limit(1 << 20)
    with open(origen, encoding="utf-8-sig", newline="") as f:
        filas = [r for r in csv.reader(f) if len(r) >= 2 and r[0].strip().lower() != "id"]
    # Se valida todo (ids y plantillas) antes de crear nada: un fallo a mitad dejaría una BD incompleta
    plantillas = [(parse_input_id(r[0]), parse_input_template(r[1])) for r in filas]
    db = BiometricDatabase(ruta_db)
    for uid, t in plantillas:
        if not db.save_template(uid, db.protector.proteger(t)):
            print(f"[AVISO] '{uid}' está repetido: se queda la primera fila")
    print(f"Importadas {len(db.get_all())} personas en '{ruta_db}'")


def exportar(db: BiometricDatabase, destino: str) -> None:
    todos = db.get_all()
    with _crear(destino) as f:
        w = csv.writer(f)
        w.writerow(["id", "template"])
        for uid, t in todos.items():
            w.writerow([uid, ";".join(f"{v:.7f}" for v in db.protector.desproteger(t))])
    print(f"Exportadas {len(todos)} personas EN CLARO a '{destino}': bórralo cuando no haga falta")


def rotar(vieja: BiometricDatabase, fichero_clave: str) -> None:
    ruta_db = vieja.csv_path
    nueva_db = ruta_db + ".rotada"
    if os.path.exists(nueva_db):
        raise ValueError(f"'{nueva_db}' ya existe: bórralo antes.")
    clave = nueva_clave()
    with _crear(fichero_clave, 0o644) as f:
        f.write(clave + "\n")
    db = BiometricDatabase(nueva_db, clave=bytes.fromhex(clave))
    pasar = lambda t: db.protector.proteger(vieja.protector.desproteger(t))
    for uid, t in vieja.get_all().items():
        db.save_template(uid, pasar(t))
    for clave_c, t in vieja.get_contrastes().items():
        db.save_contraste(clave_c[1:].rsplit("~", 1)[0], pasar(t))
    print(f"Nueva BD en '{nueva_db}' y nueva clave en '{fichero_clave}'. Sustituye las dos a la vez.")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default="database/database.csv")
    sub = p.add_subparsers(dest="orden", required=True)
    sub.add_parser("verificar")
    sub.add_parser("importar").add_argument("fichero")
    sub.add_parser("exportar").add_argument("fichero")
    sub.add_parser("rotar").add_argument("fichero")
    sub.add_parser("quitar-contrastes").add_argument("id")
    a = p.parse_args()
    try:
        if a.orden == "importar":
            importar(a.db, a.fichero)
            return 0
        db = BiometricDatabase(a.db, crear=False)
        if a.orden == "verificar":
            print(f"Íntegra: {len(db.get_all())} personas")
        elif a.orden == "exportar":
            exportar(db, a.fichero)
        elif a.orden == "rotar":
            rotar(db, a.fichero)
        else:
            print("Quitados" if db.delete_contrastes(a.id) else "Esa persona no tiene contrastes")
    except (DatabaseReadError, ClaveError, OSError, ValueError) as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
