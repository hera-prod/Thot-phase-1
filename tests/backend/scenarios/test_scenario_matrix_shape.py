"""Stub de forme pour la matrice des scénarios fonctionnels.

Vérifie simplement que `SCENARIO_MATRIX.md` existe, est non vide,
et expose les colonnes attendues (ID, Priority, Dependencies, Marker).
"""

from __future__ import annotations

from pathlib import Path

MATRIX = Path(__file__).resolve().with_name("SCENARIO_MATRIX.md")
EXPECTED_COLUMNS = ("ID", "Priority", "Dependencies", "Marker")


def _table_rows(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip().startswith("|")]


def test_scenario_matrix_exists_and_nonempty() -> None:
    assert MATRIX.is_file(), "SCENARIO_MATRIX.md manquant"
    assert MATRIX.read_text(encoding="utf-8").strip(), "matrice vide"


def test_scenario_matrix_has_expected_columns() -> None:
    text = MATRIX.read_text(encoding="utf-8")
    rows = _table_rows(text)
    assert rows, "aucune table markdown dans la matrice"
    header = [cell.strip() for cell in rows[0].strip().strip("|").split("|")]
    assert header == list(EXPECTED_COLUMNS), (
        f"colonnes inattendues: {header!r} != {list(EXPECTED_COLUMNS)!r}"
    )


def test_scenario_matrix_has_data_rows() -> None:
    text = MATRIX.read_text(encoding="utf-8")
    rows = _table_rows(text)
    # header + separator + au moins une ligne de données
    assert len(rows) >= 3, "matrice sans lignes de données"


def test_scenario_matrix_data_rows_well_formed() -> None:
    text = MATRIX.read_text(encoding="utf-8")
    rows = _table_rows(text)
    for row in rows[2:]:
        cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
        assert len(cells) == len(EXPECTED_COLUMNS), f"ligne mal formée: {row!r}"
        assert all(cells), f"cellule vide dans la ligne: {row!r}"
