"""L'invalidation de la mémoire à la suppression d'une discussion.

Une discussion supprimée ne doit plus soutenir une fiche. Le même fait soutenu
par deux discussions reste `accepted` tant qu'une autre discussion le porte :
la provenance s'accumule, elle ne remplace pas.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.db.models import (
    Base,
    Conversation,
    MemoryEntry,
    Message,
    Workspace,
)
from backend.inference import loop as L
from backend.services import activity
from backend.services import corbeille as C
from backend.services import memoire as M
from backend.services import memoire_auto as A

CONTENU = "On utilise SQLite comme base de données."


@pytest.fixture(autouse=True)
def _drapeau_reinitialise():
    activity.marquer_tour_actif(False)
    yield
    activity.marquer_tour_actif(False)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'memoire_invalidation.db'}")
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


def _espace(db, workspace_id: str = "ws_inval") -> str:
    db.add(Workspace(id=workspace_id, name="Espace"))
    db.commit()
    return workspace_id


def _discussion(db, workspace_id: str, conv_id: str) -> str:
    db.add(Conversation(id=conv_id, title=conv_id, workspace_id=workspace_id))
    db.commit()
    return conv_id


def _message(
    db,
    conv_id: str,
    message_id: str,
    contenu: str = CONTENU,
    *,
    created_at: datetime | None = None,
) -> str:
    db.add(
        Message(
            id=message_id,
            conv_id=conv_id,
            role="user",
            content=contenu,
            created_at=created_at or datetime(2026, 10, 25, 12, 0, 0),
        )
    )
    db.commit()
    return message_id


def _courante(db, workspace_id: str) -> MemoryEntry:
    return (
        db.query(MemoryEntry)
        .filter(
            MemoryEntry.workspace_id == workspace_id,
            MemoryEntry.is_current == 1,
        )
        .one()
    )


def _synthese_texte(db, workspace_id: str) -> str:
    courante = M.synthese_courante(db, workspace_id)
    assert courante is not None
    return str(courante["content"])


# ---------- 1. Une seule discussion ----------


def test_supprimer_la_seule_discussion_rend_la_fiche_stale(db):
    workspace_id = _espace(db)
    conv_id = _discussion(db, workspace_id, "conv_a")
    _message(db, conv_id, "msg_a_1")

    out = A.forcer_memoire_espace(db, workspace_id)
    assert out["fiches"]["crees"] == 1
    fiche = _courante(db, workspace_id)
    assert fiche.status == "accepted"
    assert CONTENU in _synthese_texte(db, workspace_id)

    rapport = C.mettre_en_corbeille(db, conv_id)
    assert rapport["ok"] is True

    fiche = _courante(db, workspace_id)
    assert fiche.status == "stale"
    assert CONTENU not in _synthese_texte(db, workspace_id)


# ---------- 2. Deux discussions, même fait ----------


def test_deux_discussions_meme_fait_conserve_la_fiche_jusqu_a_la_derniere(db):
    workspace_id = _espace(db)
    conv_a = _discussion(db, workspace_id, "conv_a")
    _message(db, conv_a, "msg_a_1")

    assert A.forcer_memoire_espace(db, workspace_id)["fiches"]["crees"] == 1

    conv_b = _discussion(db, workspace_id, "conv_b")
    _message(
        db,
        conv_b,
        "msg_b_1",
        created_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1),
    )
    second = A.forcer_memoire_espace(db, workspace_id)
    assert second["fiches"]["mis_a_jour"] == 1

    fiche = _courante(db, workspace_id)
    assert fiche.status == "accepted"
    assert f"discussion:{conv_a}:" in str(fiche.origin_context or "")
    assert f"discussion:{conv_b}:" in str(fiche.origin_context or "")
    assert fiche.evidence_ids == ["msg_a_1", "msg_b_1"]

    C.mettre_en_corbeille(db, conv_a)
    fiche = _courante(db, workspace_id)
    assert fiche.status == "accepted"
    assert f"discussion:{conv_a}:" not in str(fiche.origin_context or "")
    assert f"discussion:{conv_b}:" in str(fiche.origin_context or "")
    assert CONTENU in _synthese_texte(db, workspace_id)

    C.mettre_en_corbeille(db, conv_b)
    fiche = _courante(db, workspace_id)
    assert fiche.status == "stale"
    assert CONTENU not in _synthese_texte(db, workspace_id)


# ---------- 3. Idempotence ----------


def test_invalidation_idempotente_ne_cree_pas_de_versions_en_cascade(db):
    workspace_id = _espace(db)
    conv_id = _discussion(db, workspace_id, "conv_a")
    _message(db, conv_id, "msg_a_1")
    A.forcer_memoire_espace(db, workspace_id)

    fiche = _courante(db, workspace_id)
    entry_key = fiche.entry_key

    A.invalider_conversation(db, conv_id)
    A.invalider_conversation(db, conv_id)
    C.mettre_en_corbeille(db, conv_id)

    versions = db.query(MemoryEntry).filter(MemoryEntry.entry_key == entry_key).count()
    assert versions == 2
    assert _courante(db, workspace_id).status == "stale"


# ---------- 4. Purge d'une conversation mise en corbeille par l'ancien code ----------


def test_purge_invalide_aussi_une_conversation_deja_en_corbeille(db):
    workspace_id = _espace(db)
    conv_id = _discussion(db, workspace_id, "conv_a")
    _message(db, conv_id, "msg_a_1")
    A.forcer_memoire_espace(db, workspace_id)

    # Conversation mise en corbeille par une version antérieure : le marquage
    # existe, mais l'invalidation mémoire n'a jamais été jouée.
    conv = db.query(Conversation).filter(Conversation.id == conv_id).one()
    conv.deleted_at = datetime(2026, 10, 26, 12, 0, 0)
    conv.purge_after = datetime(2026, 10, 27, 12, 0, 0)
    db.commit()

    rapport = C.purger_conversation(db, conv_id, seulement_expiree=False)
    assert rapport["purged"] is True

    assert _courante(db, workspace_id).status == "stale"
    assert CONTENU not in _synthese_texte(db, workspace_id)
    assert db.query(Conversation).filter(Conversation.id == conv_id).first() is None


# ---------- 5. Une fiche invalidée par suppression ne revient pas par défaut ----------


def test_fiche_supprimee_absente_de_la_synthese_par_defaut_et_du_contexte(db):
    workspace_id = _espace(db)
    conv_id = _discussion(db, workspace_id, "conv_a")
    _message(db, conv_id, "msg_a_1")
    A.forcer_memoire_espace(db, workspace_id)
    assert CONTENU in _synthese_texte(db, workspace_id)

    C.mettre_en_corbeille(db, conv_id)

    # Le défaut de `construire_synthese` inclut les `stale` : la suppression
    # n'est pas une contradiction et ne doit pourtant jamais revenir.
    out = A.construire_synthese(db, workspace_id)
    assert out["statut"] in ("creee", "inchangee")
    synthese = _synthese_texte(db, workspace_id)
    assert "aucune orientation" in synthese.lower()
    assert CONTENU not in synthese

    # Le contexte initial de discussion ne sert pas non plus le texte en cache.
    contexte = L._synthese_espace(conv_id)
    assert CONTENU not in contexte
    assert "aucune orientation" in contexte.lower()


# ---------- 6. Invalidation : les preuves de la discussion disparaissent ----------


def test_invalidation_retire_les_preuves_des_messages_supprimes(db):
    workspace_id = _espace(db)
    conv_a = _discussion(db, workspace_id, "conv_a")
    _message(db, conv_a, "msg_a_1")
    A.forcer_memoire_espace(db, workspace_id)

    conv_b = _discussion(db, workspace_id, "conv_b")
    _message(
        db,
        conv_b,
        "msg_b_1",
        created_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1),
    )
    A.forcer_memoire_espace(db, workspace_id)
    assert _courante(db, workspace_id).evidence_ids == ["msg_a_1", "msg_b_1"]

    C.mettre_en_corbeille(db, conv_a)

    fiche = _courante(db, workspace_id)
    assert fiche.status == "accepted"
    assert fiche.evidence_ids == ["msg_b_1"]


# ---------- 7. Invalidation : une contradiction stale reste un fait révisable ----------


def test_invalidation_garde_la_contradiction_stale_et_ecarte_la_suppression(db):
    workspace_id = _espace(db)
    conv_sqlite = _discussion(db, workspace_id, "conv_sqlite")
    _message(
        db,
        conv_sqlite,
        "msg_sqlite",
        "On utilise SQLite comme base de données.",
        created_at=datetime(2026, 10, 25, 12, 0, 0),
    )
    conv_postgres = _discussion(db, workspace_id, "conv_postgres")
    _message(
        db,
        conv_postgres,
        "msg_postgres",
        "Finalement, on utilise PostgreSQL comme base de données.",
        created_at=datetime(2026, 10, 25, 12, 1, 0),
    )

    A.forcer_memoire_espace(db, workspace_id)

    # SQLite est devenue stale par CONTRADICTION ; PostgreSQL est acceptée.
    par_contenu = {
        "SQLite" if "SQLite" in row.content else "PostgreSQL": row
        for row in db.query(MemoryEntry)
        .filter(MemoryEntry.workspace_id == workspace_id, MemoryEntry.is_current == 1)
        .all()
    }
    assert par_contenu["SQLite"].status == "stale"
    assert (par_contenu["SQLite"].decision or {}).get("reason") == (
        "remplacée par une décision plus récente"
    )
    assert par_contenu["PostgreSQL"].status == "accepted"

    # La corbeille invalide PostgreSQL pour SUPPRESSION, pas SQLite.
    C.mettre_en_corbeille(db, conv_postgres)

    par_contenu = {
        "SQLite" if "SQLite" in row.content else "PostgreSQL": row
        for row in db.query(MemoryEntry)
        .filter(MemoryEntry.workspace_id == workspace_id, MemoryEntry.is_current == 1)
        .all()
    }
    assert par_contenu["SQLite"].status == "stale"
    assert (par_contenu["SQLite"].decision or {}).get("reason") == (
        "remplacée par une décision plus récente"
    )
    assert par_contenu["PostgreSQL"].status == "stale"
    assert (par_contenu["PostgreSQL"].decision or {}).get("reason") == (
        "discussion supprimée"
    )

    synthese = _synthese_texte(db, workspace_id)
    assert "SQLite" in synthese
    assert "PostgreSQL" not in synthese

    # Un forçage sans nouveaux messages ne réintroduit pas la fiche supprimée.
    out = A.forcer_memoire_espace(db, workspace_id)
    assert out["statut"] in ("rien_a_promouvoir", "effectue")
    synthese = _synthese_texte(db, workspace_id)
    assert "SQLite" in synthese
    assert "PostgreSQL" not in synthese
