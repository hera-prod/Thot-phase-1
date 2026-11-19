"""Vocabulaire de la fiche de mémoire — Phase 6, T6.2.

Six catégories et quatre statuts d'affirmation, déclarés au service et au
modèle. Le micro-test vise le vocabulaire lui-même : les constantes, les colonnes
du modèle et la validation posée à la proposition. La migration v8 est couverte
dans `tests/backend/test_migrations.py`.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.db.models import Base, MemoryEntry, Workspace
from backend.services import memoire as M


def test_vocabulary_memoire_contient_six_categories() -> None:
    """T6.2 — six catégories de faits documentées, sans valeur fantaisiste."""
    assert M._MEMORY_CATEGORIES == (
        "contraintes",
        "decisions",
        "contexte_stable",
        "preferences",
        "connaissances_de_travail",
        "reperes_documentaires",
    )


def test_vocabulary_memoire_contient_quatre_statuts_d_affirmation() -> None:
    """T6.2 — quatre statuts d'affirmation, distincts des statuts de cycle."""
    assert M._MEMORY_AFFIRMATION_STATUSES == (
        "decision",
        "hypothese",
        "observation",
        "information_a_verifier",
    )


def test_memory_entry_porte_le_vocabulaire_des_fiches() -> None:
    """T6.2 — le modèle expose catégorie et affirmation_status comme
    colonnes NULLables, sans forcer le vocabulaire sur les fiches legacy."""
    colonnes = MemoryEntry.__table__.columns
    assert "category" in colonnes
    assert "affirmation_status" in colonnes
    assert colonnes["category"].nullable is True
    assert colonnes["affirmation_status"].nullable is True


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """Session sur base temporaire, injectée comme SessionLocal du projet."""
    engine = create_engine(f"sqlite:///{tmp_path / 'memoire_vocabulary.db'}")
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    monkeypatch.setattr("backend.db.database.SessionLocal", TestSession)
    s = TestSession()
    try:
        yield s
    finally:
        s.close()


def _espace(db) -> str:
    """Espace actif minimal, pour que `propose` puisse l'utiliser."""
    ws = Workspace(id="ws_vocabulary", name="Vocabulary")
    db.add(ws)
    db.commit()
    return str(ws.id)


def test_propose_accepte_et_persiste_le_vocabulaire(db) -> None:
    """T6.2 — le vocabulaire transite par la proposition, validé et persistant."""
    ws_id = _espace(db)
    out = M.propose(
        db,
        ws_id,
        "Le projet utilise SQLite pour le registre local.",
        category="decisions",
        affirmation_status="decision",
    )
    assert out["ok"] is True and out["created"] is True
    row = db.query(MemoryEntry).filter(MemoryEntry.id == out["entry_id"]).first()
    assert row.category == "decisions"
    assert row.affirmation_status == "decision"


def test_propose_refuse_un_vocabulaire_inconnu(db) -> None:
    """T6.2 — un libellé hors vocabulaire est un refus immédiat, pas un silence."""
    ws_id = _espace(db)
    with pytest.raises(ValueError, match=r"\[ENUM_INVALID\]"):
        M.propose(db, ws_id, "Le projet utilise SQLite.", category="nimporte_quoi")


def test_propose_refuse_un_statut_d_affirmation_inconnu(db) -> None:
    """T6.2 — un statut d'affirmation hors vocabulaire est un refus immédiat."""
    ws_id = _espace(db)
    with pytest.raises(ValueError, match=r"\[ENUM_INVALID\]"):
        M.propose(
            db, ws_id, "Le projet utilise SQLite.", affirmation_status="certitude"
        )
