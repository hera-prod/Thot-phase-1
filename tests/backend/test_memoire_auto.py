"""tests/backend/test_memoire_auto.py — la mémoire d'espace déterministe.

Aucun LLM n'est chargé : l'extraction classe des phrases, l'écriture passe par
`proposer_automatique`, et la synthèse est le gabarit de `build_space_synthesis`.
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
    MemorySynthesis,
    Message,
    Workspace,
)
from backend.services import activity, memoire as M
from backend.services import memoire_auto as A


@pytest.fixture(autouse=True)
def _drapeau_reinitialise():
    activity.marquer_tour_actif(False)
    A._DERNIER_ETAT.clear()
    yield
    activity.marquer_tour_actif(False)
    with activity._OWNER_LOCK:
        activity._OWNER = None
    try:
        activity._RESERVATION_LOCK.release()
    except RuntimeError:
        pass
    A._DERNIER_ETAT.clear()


@pytest.fixture()
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'memoire_auto.db'}")
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


def _espace(db, workspace_id: str = "ws_auto") -> str:
    db.add(Workspace(id=workspace_id, name="Espace"))
    db.add(
        Conversation(
            id=f"conv_{workspace_id}", title="Projet", workspace_id=workspace_id
        )
    )
    db.commit()
    return workspace_id


def _message(
    db,
    workspace_id: str,
    index: int,
    contenu: str,
    *,
    role: str = "user",
    created_at: datetime | None = None,
) -> str:
    mid = f"msg_{workspace_id}_{index}"
    db.add(
        Message(
            id=mid,
            conv_id=f"conv_{workspace_id}",
            role=role,
            content=contenu,
            created_at=created_at
            or datetime(2026, 10, 25, 12, 0, 0) + timedelta(seconds=index),
        )
    )
    db.commit()
    return mid


def _messages(db, workspace_id: str):
    return A.collecter_messages(db, workspace_id)


# ---------- 1. Extraction positive ----------


def test_extraction_positive_classes_et_provenance(db):
    workspace_id = _espace(db)
    _message(db, workspace_id, 1, "On utilise SQLite comme base de données.")
    _message(db, workspace_id, 2, "Le projet doit rester sous 8 Go de RAM.")
    _message(db, workspace_id, 3, "Je préfère des réponses courtes et structurées.")
    _message(
        db,
        workspace_id,
        4,
        "Le cahier des charges est le document de référence à consulter.",
    )

    faits = A.extraire_faits(_messages(db, workspace_id), workspace_id=workspace_id)

    assert len(faits) == 4
    categories = {f.category for f in faits}
    assert categories == {
        "decisions",
        "contraintes",
        "preferences",
        "reperes_documentaires",
    }
    assert {f.affirmation_status for f in faits} == {
        "decision",
        "information_a_verifier",
    }
    for fait in faits:
        assert fait.origin_conversation_id == f"conv_{workspace_id}"
        assert fait.origin_context.startswith("discussion:")
        assert ":message:" in fait.origin_context
        assert fait.evidence_ids == [fait.message_id]


# ---------- 2. Négatifs ----------


def test_negatifs_ne_produisent_aucun_fait(db):
    workspace_id = _espace(db)
    _message(
        db,
        workspace_id,
        1,
        "Quel est le projet et quelle est sa base de données actuelle ?",
    )
    _message(db, workspace_id, 2, "Bonjour !")
    _message(db, workspace_id, 3, "```python\nprint('on utilise SQLite')\n```")
    _message(db, workspace_id, 4, "Ok.")
    _message(
        db,
        workspace_id,
        5,
        "On utilise SQLite comme base de données.",
        role="assistant",
    )

    assert (
        A.extraire_faits(_messages(db, workspace_id), workspace_id=workspace_id) == []
    )


# ---------- 3. Déduplication dans le lot ----------


def test_meme_fait_deux_messages_une_seule_fiche(db):
    workspace_id = _espace(db)
    contenu = "On utilise SQLite comme base de données."
    _message(db, workspace_id, 1, contenu)
    _message(db, workspace_id, 2, contenu)

    out = A.entretenir_memoire(db, workspace_id, force=True)

    assert out["statut"] == "effectue"
    assert out["faits"] == 1
    assert (
        db.query(MemoryEntry).filter(MemoryEntry.workspace_id == workspace_id).count()
        == 1
    )


def test_meme_fait_deux_discussions_conserve_les_deux_origines(db):
    workspace_id = _espace(db)
    contenu = "On utilise SQLite comme base de données."
    _message(db, workspace_id, 1, contenu)

    premier = A.entretenir_memoire(db, workspace_id, force=True)
    assert premier["fiches"]["crees"] == 1

    # Seconde discussion, message postérieur à la première passe : seule sa
    # provenance doit être collectée cette fois.
    db.add(Conversation(id="conv_autre", title="Autre", workspace_id=workspace_id))
    db.add(
        Message(
            id="msg_autre_1",
            conv_id="conv_autre",
            role="user",
            content=contenu,
            created_at=datetime.now(timezone.utc).replace(tzinfo=None)
            + timedelta(hours=1),
        )
    )
    db.commit()

    second = A.entretenir_memoire(db, workspace_id, force=True)

    assert second["fiches"]["mis_a_jour"] == 1
    assert second["fiches"]["doublons"] == 0
    fiche = (
        db.query(MemoryEntry)
        .filter(MemoryEntry.workspace_id == workspace_id, MemoryEntry.is_current == 1)
        .one()
    )
    assert f"conv_{workspace_id}" in fiche.origin_context
    assert "conv_autre" in fiche.origin_context
    assert f"msg_{workspace_id}_1" in fiche.evidence_ids
    assert "msg_autre_1" in fiche.evidence_ids
    assert len(fiche.evidence_ids) == 2


# ---------- 4. Exclusion au point d'écriture ----------


def test_exclusion_empeche_la_recreation(db):
    workspace_id = _espace(db)
    _message(db, workspace_id, 1, "On utilise SQLite comme base de données.")
    premier = A.entretenir_memoire(db, workspace_id, force=True)
    assert premier["fiches"]["crees"] == 1

    fiche = (
        db.query(MemoryEntry)
        .filter(MemoryEntry.workspace_id == workspace_id, MemoryEntry.is_current == 1)
        .first()
    )
    M.purge_memory(db, fiche.id, workspace_id, exclure=True)

    ws = db.query(Workspace).filter(Workspace.id == workspace_id).first()
    ws.memory_last_processed_at = None
    db.commit()

    second = A.entretenir_memoire(db, workspace_id, force=True)
    assert second["fiches"]["exclus"] == 1
    assert second["fiches"]["crees"] == 0
    assert (
        db.query(MemoryEntry).filter(MemoryEntry.workspace_id == workspace_id).count()
        == 0
    )


# ---------- 5. Contradiction : l'ancienne décision devient stale ----------


def test_contradiction_rend_l_ancienne_fiche_stale(db):
    workspace_id = _espace(db)
    _message(db, workspace_id, 1, "On utilise SQLite.")
    _message(db, workspace_id, 2, "Finalement, on utilise PostgreSQL.")

    A.entretenir_memoire(db, workspace_id, force=True)

    courantes = (
        db.query(MemoryEntry)
        .filter(MemoryEntry.workspace_id == workspace_id, MemoryEntry.is_current == 1)
        .all()
    )
    par_contenu = {row.content: row for row in courantes}
    assert par_contenu["On utilise SQLite."].status == "stale"
    assert par_contenu["Finalement, on utilise PostgreSQL."].status == "accepted"
    assert (par_contenu["On utilise SQLite."].decision or {}).get(
        "reason"
    ) == "remplacée par une décision plus récente"


# ---------- 6. Espace vide ----------


def test_espace_vide_ne_cree_aucune_synthese(db):
    workspace_id = _espace(db)

    out = A.forcer_memoire_espace(db, workspace_id)

    assert out["statut"] == "rien_a_promouvoir"
    assert out["synthese"]["statut"] == "aucune_synthese"
    assert (
        db.query(MemorySynthesis)
        .filter(MemorySynthesis.workspace_id == workspace_id)
        .count()
        == 0
    )


# ---------- 7. Forçage : un seul tour suffit ----------


def test_force_traite_meme_avec_un_tour(db):
    workspace_id = _espace(db)
    _message(db, workspace_id, 1, "On utilise SQLite comme base de données.")
    ws = db.query(Workspace).filter(Workspace.id == workspace_id).first()
    ws.auto_memory_turn_count = 1
    db.commit()

    out = A.entretenir_memoire(db, workspace_id, force=True)

    assert out["statut"] == "effectue"
    assert out["fiches"]["crees"] == 1
    assert (
        db.query(MemoryEntry).filter(MemoryEntry.workspace_id == workspace_id).count()
        == 1
    )


# ---------- 8. Déclencheurs ----------


def test_auto_effectue_apres_cinq_tours_et_six_minutes(db):
    workspace_id = _espace(db)
    _message(db, workspace_id, 1, "On utilise SQLite comme base de données.")
    maintenant = datetime(2026, 10, 25, 12, 10, 0)
    ws = db.query(Workspace).filter(Workspace.id == workspace_id).first()
    ws.auto_memory_turn_count = 5
    ws.last_activity_at = maintenant - timedelta(minutes=6)
    db.commit()

    out = A.entretenir_memoire(db, workspace_id, maintenant=maintenant)

    assert out["statut"] == "effectue"
    assert out["fiches"]["crees"] == 1
    assert ws.auto_memory_turn_count == 0
    assert ws.memory_last_processed_at == maintenant


def test_auto_pas_encore_avec_un_tour(db):
    workspace_id = _espace(db)
    _message(db, workspace_id, 1, "On utilise SQLite comme base de données.")
    maintenant = datetime(2026, 10, 25, 12, 10, 0)
    ws = db.query(Workspace).filter(Workspace.id == workspace_id).first()
    ws.auto_memory_turn_count = 1
    ws.last_activity_at = maintenant - timedelta(minutes=6)
    db.commit()

    out = A.entretenir_memoire(db, workspace_id, maintenant=maintenant)

    assert out["statut"] == "pas_encore"
    assert out["raison"] == "seuil_tours"


def test_tour_actif_differe_l_entretien(db):
    workspace_id = _espace(db)
    _message(db, workspace_id, 1, "On utilise SQLite comme base de données.")
    activity.marquer_tour_actif(True)

    out = A.entretenir_memoire(db, workspace_id, force=True)

    assert out["ok"] is False
    assert out["statut"] == "differe"
    assert out["raison"] == "tour_actif"


# ---------- 9. Synthèse : recalcul, correction, suppression ----------


def test_synthese_suit_les_faits_jusqu_a_leur_suppression(db):
    workspace_id = _espace(db)
    _message(db, workspace_id, 1, "On utilise SQLite comme base de données.")
    _message(db, workspace_id, 2, "Le projet doit rester sous 8 Go de RAM.")
    A.entretenir_memoire(db, workspace_id, force=True)

    courante = M.synthese_courante(db, workspace_id)
    assert courante is not None
    assert "SQLite" in courante["content"]
    assert "8 Go" in courante["content"]
    assert len(courante["source_entry_ids"]) == 2

    fiche = (
        db.query(MemoryEntry)
        .filter(
            MemoryEntry.workspace_id == workspace_id,
            MemoryEntry.is_current == 1,
            MemoryEntry.content.like("%SQLite%"),
        )
        .first()
    )
    M.correct(db, fiche.id, "On utilise PostgreSQL comme base de données.")
    out = A.construire_synthese(db, workspace_id)
    assert out["statut"] == "creee"
    courante = M.synthese_courante(db, workspace_id)
    assert "PostgreSQL" in courante["content"]
    assert "SQLite" not in courante["content"]

    M.purge_memory(db, fiche.id, workspace_id)
    out = A.construire_synthese(db, workspace_id)
    assert out["statut"] == "creee"
    courante = M.synthese_courante(db, workspace_id)
    assert "PostgreSQL" not in courante["content"]
    assert "8 Go" in courante["content"]


# ---------- 10. Activité et fin de tour ----------


def test_fin_de_tour_incremente_mais_activite_non(db):
    workspace_id = _espace(db)

    activite = A.enregistrer_activite_espace(db, f"conv_{workspace_id}", workspace_id)
    ws = db.query(Workspace).filter(Workspace.id == workspace_id).first()
    assert activite["ok"] is True
    assert ws.auto_memory_turn_count == 0
    assert ws.last_activity_at is not None

    fin = A.finaliser_tour_reussi(db, f"conv_{workspace_id}", workspace_id)
    assert fin["ok"] is True
    assert ws.auto_memory_turn_count == 1
    assert fin["tours"] == 1


# ---------- 11. Redémarrage : la borne traitée est durable ----------


def test_redemarrage_ne_reduplique_pas(db):
    workspace_id = _espace(db)
    _message(
        db,
        workspace_id,
        1,
        "On utilise SQLite comme base de données.",
        created_at=datetime(2026, 10, 25, 9, 0, 0),
    )
    ws = db.query(Workspace).filter(Workspace.id == workspace_id).first()
    ws.last_activity_at = datetime(2026, 10, 25, 9, 5, 0)
    db.commit()
    A.entretenir_memoire(db, workspace_id, force=True)
    assert (
        db.query(MemoryEntry).filter(MemoryEntry.workspace_id == workspace_id).count()
        == 1
    )

    second = A.entretenir_memoire(
        db, workspace_id, force=True, maintenant=datetime(2026, 10, 25, 12, 0, 0)
    )

    assert second["statut"] == "rien_a_promouvoir"
    assert (
        db.query(MemoryEntry).filter(MemoryEntry.workspace_id == workspace_id).count()
        == 1
    )


# ---------- 12. Contradiction avant déduplication ----------


def test_contradiction_longue_prevaut_sur_la_deduplication(db):
    workspace_id = _espace(db)
    _message(
        db,
        workspace_id,
        1,
        "Pour ce projet nous allons utiliser SQLite dans toute l'application et pour toutes les données.",
    )
    _message(
        db,
        workspace_id,
        2,
        "Pour ce projet nous allons finalement utiliser PostgreSQL dans toute l'application et pour toutes les données.",
    )

    A.entretenir_memoire(db, workspace_id, force=True)

    courantes = (
        db.query(MemoryEntry)
        .filter(MemoryEntry.workspace_id == workspace_id, MemoryEntry.is_current == 1)
        .all()
    )
    par_contenu = {row.content: row for row in courantes}
    sqlite = next(row for contenu, row in par_contenu.items() if "SQLite" in contenu)
    postgres = next(
        row for contenu, row in par_contenu.items() if "PostgreSQL" in contenu
    )
    assert sqlite.status == "stale"
    assert postgres.status == "accepted"


# ---------- 13. Déduplication intra-lot : provenance multi-discussion ----------


def test_meme_fait_deux_discussions_dans_une_seule_passe(db):
    workspace_id = _espace(db)
    contenu = "On utilise SQLite comme base de données."
    _message(db, workspace_id, 1, contenu)
    db.add(Conversation(id="conv_seconde", title="Seconde", workspace_id=workspace_id))
    db.add(
        Message(
            id="msg_seconde_1",
            conv_id="conv_seconde",
            role="user",
            content=contenu,
            created_at=datetime(2026, 10, 25, 12, 0, 1),
        )
    )
    db.commit()

    faits = A.extraire_faits(_messages(db, workspace_id), workspace_id=workspace_id)
    assert len(faits) == 1
    assert f"conv_{workspace_id}" in faits[0].origin_context
    assert "conv_seconde" in faits[0].origin_context

    out = A.entretenir_memoire(db, workspace_id, force=True)

    assert out["faits"] == 1
    fiche = (
        db.query(MemoryEntry)
        .filter(MemoryEntry.workspace_id == workspace_id, MemoryEntry.is_current == 1)
        .one()
    )
    assert f"conv_{workspace_id}" in fiche.origin_context
    assert "conv_seconde" in fiche.origin_context
    assert len(fiche.evidence_ids) == 2


# ---------- 14. Une question sans « ? » n'est pas un fait ----------


def test_question_sans_point_interrogatif_ne_produit_aucun_fait(db):
    workspace_id = _espace(db)
    _message(
        db,
        workspace_id,
        1,
        "Peux-tu me dire quelle base on utilise pour le stockage des données du projet",
    )
    _message(
        db,
        workspace_id,
        2,
        "Je voudrais savoir si le projet doit rester sous 8 Go de RAM",
    )
    _message(db, workspace_id, 3, "Quel est le but du projet")

    assert (
        A.extraire_faits(_messages(db, workspace_id), workspace_id=workspace_id) == []
    )


# ---------- 15. Répétition littérale : la fiche accepted ne se périme pas ----------


def test_repetition_litterale_ne_perime_pas_la_fiche_accepted(db):
    workspace_id = _espace(db)
    sqlite = (
        "Pour ce projet nous allons utiliser SQLite dans toute l'application "
        "et pour toutes les données."
    )
    postgres = (
        "Pour ce projet nous allons finalement utiliser PostgreSQL dans toute "
        "l'application et pour toutes les données."
    )
    _message(db, workspace_id, 1, sqlite)
    _message(db, workspace_id, 2, postgres)

    A.entretenir_memoire(db, workspace_id, force=True)

    # Seconde passe : la MÊME décision PostgreSQL est répétée. Elle contient
    # « finalement », donc le détecteur de contradiction la voit — mais elle ne
    # doit pas périmer la fiche qu'elle répète.
    _message(
        db,
        workspace_id,
        3,
        postgres,
        created_at=datetime.now(timezone.utc).replace(tzinfo=None)
        + timedelta(minutes=5),
    )
    A.entretenir_memoire(db, workspace_id, force=True)

    courantes = (
        db.query(MemoryEntry)
        .filter(MemoryEntry.workspace_id == workspace_id, MemoryEntry.is_current == 1)
        .all()
    )
    postgres_courantes = [row for row in courantes if "PostgreSQL" in row.content]
    sqlite_courantes = [row for row in courantes if "SQLite" in row.content]

    assert len(postgres_courantes) == 1
    assert postgres_courantes[0].status == "accepted"
    assert len(sqlite_courantes) == 1
    assert sqlite_courantes[0].status == "stale"
    # Aucune version stale n'est née de la répétition littérale.
    assert (
        db.query(MemoryEntry)
        .filter(
            MemoryEntry.entry_key == postgres_courantes[0].entry_key,
            MemoryEntry.status == "stale",
        )
        .count()
        == 0
    )


# ---------- 16. Le forçage manuel passe par le wrapper et émet ses événements ----


def test_forcer_memoire_espace_emet_started_puis_finished(db, monkeypatch):
    workspace_id = _espace(db)
    _message(db, workspace_id, 1, "On utilise SQLite comme base de données.")
    evenements: list[tuple[str, dict]] = []

    def fake_emit(event, payload, **kwargs):
        evenements.append((event, payload))
        return True

    monkeypatch.setattr("backend.inference.ipc.emit", fake_emit)

    out = A.forcer_memoire_espace(db, workspace_id)

    assert out["statut"] == "effectue"
    assert [event for event, _ in evenements] == ["memory_started", "memory_finished"]
    assert evenements[0][1]["mode"] == "force"
    assert evenements[0][1]["kind"] == "memory"
    assert evenements[1][1]["statut"] == "effectue"
    assert evenements[1][1]["faits"] == 1
    assert activity.try_reserver("chat", "chat:libre") is not None


# ---------- 17. `executer_entretien` passe par le wrapper, pas par le cœur -------


def test_executer_entretien_appelle_le_wrapper(db, monkeypatch):
    workspace_id = _espace(db)
    maintenant = datetime(2026, 10, 25, 12, 10, 0)
    appels: list[tuple[str, dict]] = []

    def faux_wrapper(db_arg, ws, **kwargs):
        appels.append((ws, kwargs))
        return {
            "ok": True,
            "statut": "effectue",
            "messages": 0,
            "faits": 0,
            "fiches": {},
            "synthese": {},
        }

    monkeypatch.setattr(A, "executer_entretien_memoire", faux_wrapper)

    out = M.executer_entretien(
        db,
        workspace_id,
        dernier_interaction=maintenant - timedelta(minutes=6),
        maintenant=maintenant,
    )

    assert appels == [
        (
            workspace_id,
            {"force": False, "mode": "auto", "maintenant": maintenant},
        )
    ]
    assert out["statut"] == "effectue"
    assert out["admis"] is True
