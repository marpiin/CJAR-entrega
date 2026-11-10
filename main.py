"""
Demo por terminal: inscribe, verifica e identifica templates sintéticos con los
métodos _t y comprueba que se rechazan templates inválidos.

Usa una BD aparte (database/demo_database.csv), no toca la oficial.
"""

import os
import numpy as np
from src.biometric_system import BiometricSystem


def run_demo():
    print("=" * 60)
    print("CJAR - demo del sistema biométrico")
    print("=" * 60)

    db_test_path = "database/demo_database.csv"
    if os.path.exists(db_test_path):
        os.remove(db_test_path)
    system = BiometricSystem(db_path=db_test_path, threshold=0.48)
    print(f"BD de la demo: {db_test_path}")

    rng = np.random.default_rng(seed=42)
    t_persona1 = rng.standard_normal(512).astype(np.float32)
    t_persona1 /= np.linalg.norm(t_persona1)
    t_persona2 = rng.standard_normal(512).astype(np.float32)
    t_persona2 /= np.linalg.norm(t_persona2)

    vector_nan = t_persona1.copy()
    vector_nan[0] = np.nan

    # (descripción, resultado, esperado)
    pruebas = [
        ("Inscribir persona_01", system.inscribir_t("persona_01", t_persona1), True),
        ("Inscribir persona_02", system.inscribir_t("persona_02", t_persona2), True),
        ("Verificar persona_01 con su template", system.verificar_t("persona_01", t_persona1), True),
        ("Verificar persona_01 con el de persona_02", system.verificar_t("persona_01", t_persona2), False),
        ("Identificar el template de persona_01", system.identificar_t(t_persona1), "persona_01"),
        ("Inscribir un vector de 10 valores", system.inscribir_t("hacker", np.full(10, 0.5, np.float32)), False),
        ("Verificar con un vector con NaN", system.verificar_t("persona_01", vector_nan), False),
    ]

    fallos = 0
    for descripcion, resultado, esperado in pruebas:
        ok = resultado == esperado
        fallos += not ok
        print(f"  [{'OK' if ok else 'FALLO'}] {descripcion}: {resultado!r} (esperado {esperado!r})")

    print("=" * 60)
    print(f"{len(pruebas) - fallos}/{len(pruebas)} pruebas correctas")


if __name__ == "__main__":
    run_demo()
