"""Socle minimal d'exécution pour les scénarios fonctionnels (non lents).

Les scénarios réutilisent la discipline de `tests/backend/test_sf_vie_03_travail_tardif.py` :
base SQLite jetable par test, `SessionLocal` du projet redirigé, ingestion neutralisée
(aucun fil réel). Ce module centralise ces gestes pour éviter de les recopier,
et fournit un marqueur `scenario` utilisable sans déclarer de dépendance lourde.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.db import database as database_module
from backend.db.models import Base


def create_isolated_db(
    tmp_path,
    *,
    name: str = "scenario.db",
    app_dir_name: str = "app",
    monkeypatch: Any = None,
    neutralize_ingestion: bool = True,
    attachment_ingest_module: Any = None,
):
    """Crée une base SQLite neuve et jetable pour un scénario.

    Retourne `(engine, sessions, db_path)`. Si `monkeypatch` est fourni, redirige
    `backend.db.database.SessionLocal` et l'environnement comme le fait la fixture
    historique ``base`` ; l'ingestion réelle est neutralisée par défaut.
    """
    tmp_path = Path(tmp_path)
    db_path = tmp_path / name

    # Ne rien laisser d'une exécution précédente (WAL/SHM inclus)
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            Path(str(db_path) + suffix).unlink(missing_ok=True)
        except OSError:
            pass

    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    from backend.db.database import _ensure_fts

    _ensure_fts(engine)
    sessions = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )

    if monkeypatch is not None:
        monkeypatch.setattr(database_module, "SessionLocal", sessions)
        monkeypatch.setenv("THOT_PORTABLE", "1")
        monkeypatch.setenv("THOT_APP_DATA_DIR", str(tmp_path / app_dir_name))
        if neutralize_ingestion:
            try:
                if attachment_ingest_module is None:
                    from backend.ingestion import attachment_ingest as _ai

                    attachment_ingest_module = _ai
                monkeypatch.setattr(
                    attachment_ingest_module, "lancer_ingestion", lambda ids, **kw: None
                )
            except Exception:
                pass

    return engine, sessions, db_path


def _remove_db_files(db_path) -> None:
    db_path = Path(db_path)
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            Path(str(db_path) + suffix).unlink(missing_ok=True)
        except OSError:
            pass


def cleanup_temp_file(db_path, *, engine=None):
    """Nettoie le fichier temporaire (et le moteur associé) d'un scénario."""
    if engine is not None:
        try:
            engine.dispose()
        except Exception:
            pass
    _remove_db_files(db_path)
    return None


# Alias historiques / noms alternatifs pour rester découvrables.
cleanup_temp_db = cleanup_temp_file
cleanup_db_file = cleanup_temp_file
remove_temp_file = cleanup_temp_file


def scenario_marker(name=None, *, slow: bool = False):
    """Fabrique un marqueur de scénario.

    Utilisable comme décorateur (``@scenario_marker("SF-VIE-03")``) ou affecté à
    ``pytestmark``. Le marqueur under-jacent est ``pytest.mark.scenario(name)``.
    """
    mark = pytest.mark.scenario(name)
    if not slow:
        return mark

    def _apply(obj):
        return pytest.mark.slow(pytest.mark.scenario(name)(obj))

    return _apply


# Alias pour compatibilité avec d'autres conventions de nommage.
scenario = scenario_marker
mark_scenario = scenario_marker
make_scenario_marker = scenario_marker
create_scenario_marker = scenario_marker


# Export explicite pour faciliter l'introspection.
__all__ = [
    "create_isolated_db",
    "cleanup_temp_file",
    "cleanup_temp_db",
    "cleanup_db_file",
    "remove_temp_file",
    "scenario_marker",
    "scenario",
    "mark_scenario",
    "make_scenario_marker",
    "create_scenario_marker",
]
