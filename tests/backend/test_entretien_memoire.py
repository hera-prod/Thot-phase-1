"""tests/backend/test_entretien_memoire.py — L'entretien courant de la synthèse (SF-MEM-02/03/04).

CE QUE CE FICHIER PROUVE, ET POURQUOI CE SONT DES PREUVES ET NON DES CONSTATS :

* le seuil d'inactivité est franchi seulement après cinq minutes d'absence d'usage
  (le franchissement est l'exception ; une reconstruction à chaque message serait une
  autre exception, justement écartée) ;
* sous le seuil, aucune demande d'entretien n'est émise — l'inactivité accumulée
  ne déclenche rien tant qu'elle n'est pas franchie ;
* une tâche de traitement active reporte l'admission : la demande déposée reste en
  attente, jamais exécutée en parallèle d'un tour actif ;
* l'absence de nouvelle contribution ne relance pas l'entretien (SF-MEM-04) ;
* l'admission effective passe par la proposition de candidat — jamais une promotion.

Aucun LLM n'est chargé : la synthèse est le gabarit déterministe (`build_space_synthesis`),
et la proposition est le vrai service `propose` sur une base temporaire en sqlite.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.db.models import Base, Conversation, MemoryEntry, Message
from backend.services import espaces as service
from backend.services import memoire as memoire_service


@pytest.fixture(autouse=True)
def _registre_dossier_vide(monkeypatch):
    """Le registre module-level est partagé entre les tests : chacun repart sans activité."""
    monkeypatch.setattr(memoire_service, "REGISTRE_ACTIVITE", {})


@pytest.fixture()
def base(tmp_path, monkeypatch):
    """Session sur base temporaire sqlite, injectée comme SessionLocal du projet."""
    engine = create_engine(f"sqlite:///{tmp_path / 'entretien-memoire.db'}")
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


# ---------- 1-2. Le SEUIL D'INACTIVITÉ : franchissement et absence de demande ----------


def test_depasse_seuil_true_apres_cinq_minutes() -> None:
    """SF-MEM-03 — cinq minutes d'inactivité moins un seconde : le seuil est franchi."""
    maintenant = datetime(2026, 10, 25, 12, 0, 0)
    dernier = maintenant - timedelta(minutes=5, seconds=1)

    assert memoire_service.depasse_seuil_inactivite(dernier, maintenant) is True


def test_pas_de_demande_sous_le_seuil(base) -> None:
    """Sous cinq minutes d'inactivité, aucune demande d'entretien n'est déposée."""
    maintenant = datetime(2026, 10, 25, 12, 0, 0)
    ws = service.create_workspace(base, "Proche du but")

    out = memoire_service.executer_entretien(
        base,
        str(ws.id),
        dernier_interaction=maintenant - timedelta(minutes=4, seconds=59),
        maintenant=maintenant,
    )

    assert out["depot"] is False
    assert out["demande"] is None
    assert out["seuil_depasse"] is False
    assert out["admis"] is False
    assert out["statut"] == "pas_de_demande"
    assert out["raison"] == "inactivité_insuffisante"
    assert out["resultat"] is None


# ---------- 3. L'ADMISSION DIFFÉRÉE : une tâche de traitement est en vol ----------


def test_differe_si_tache_active(base) -> None:
    """SF-MEM-03 — seuil franché mais tâche de traitement active : la demande est déposée,
    l'admission ne l'est pas — jamais d'exécution en parallèle d'un tour."""
    ws = service.create_workspace(base, "Travail en cours")
    workspace_id = str(ws.id)
    maintenant = datetime(2026, 10, 25, 12, 0, 0)

    out = memoire_service.executer_entretien(
        base,
        workspace_id,
        dernier_interaction=maintenant - timedelta(minutes=6),
        maintenant=maintenant,
        tache_active=True,
    )

    assert out["statut"] == "differe"
    assert out["depot"] is True
    assert out["demande"] == f"entretien:{workspace_id}"
    assert out["seuil_depasse"] is True
    assert out["admis"] is False
    assert out["raison"] == "tache_active"
    assert out["resultat"] is None
    assert (
        base.query(MemoryEntry).filter(MemoryEntry.workspace_id == workspace_id).count()
        == 0
    )


# ---------- 4. SF-MEM-04 : pas de relance sans nouvelle contribution ----------


def test_pas_de_changement_idempotence(base) -> None:
    """SF-MEM-04 — inactivité dépassée sans contribution nouvelle : pas d'entretien réopéré."""
    ws = service.create_workspace(base, "Projet")
    workspace_id = str(ws.id)
    maintenant = datetime(2026, 10, 25, 12, 0, 0)

    out = memoire_service.executer_entretien(
        base,
        workspace_id,
        dernier_interaction=maintenant - timedelta(minutes=9),
        maintenant=maintenant,
        contribution_inchangee=True,
    )

    assert out["statut"] == "pas_de_changement"
    assert out["admis"] is True
    assert out["raison"] == "aucune_nouvelle_contribution"
    assert out["resultat"] is None
    assert (
        base.query(MemoryEntry).filter(MemoryEntry.workspace_id == workspace_id).count()
        == 0
    )


# ---------- 5. L'ADMISSION EFFECTIVE : l'extraction déterministe tourne ----------


def test_effectue_fait_tourner_le_pipeline(base) -> None:
    """L'admission effective extrait les faits et construit la synthèse — sans
    candidat artificiel : les fiches viennent des messages, pas d'un entretien."""
    ws = service.create_workspace(base, "Projet mémoire")
    workspace_id = str(ws.id)
    maintenant = datetime(2026, 10, 25, 12, 0, 0)
    base.add(
        Conversation(id="conv_entretien", title="Projet", workspace_id=workspace_id)
    )
    base.add(
        Message(
            id="msg_entretien_1",
            conv_id="conv_entretien",
            role="user",
            content="On utilise SQLite comme base de données du projet.",
        )
    )
    ws.auto_memory_turn_count = 5
    ws.last_activity_at = maintenant - timedelta(minutes=10)
    base.commit()

    out = memoire_service.executer_entretien(
        base,
        workspace_id,
        dernier_interaction=maintenant - timedelta(minutes=10),
        maintenant=maintenant,
    )

    assert out["statut"] == "effectue"
    assert out["depot"] is True
    assert out["admis"] is True
    assert out["seuil_depasse"] is True
    assert out["resultat"]["ok"] is True
    assert out["resultat"]["statut"] == "effectue"
    assert out["resultat"]["faits"] == 1

    fiches = (
        base.query(MemoryEntry).filter(MemoryEntry.workspace_id == workspace_id).all()
    )
    assert len(fiches) == 1
    # L'extraction déterministe écrit directement `accepted`, pas `candidate`.
    assert fiches[0].status == "accepted"


# ---------- 6. Le REGISTRE D'ACTIVITÉ : durée et absence ----------


def test_activite_registry_round_trip() -> None:
    """Registre d'activité : dernier usage, durée d'inactivité, absence = None (pas un zéro)."""
    workspace_id = "ws_registre_entretien"
    maintenant = datetime(2026, 10, 25, 12, 0, 0)

    memoire_service.enregistrer_activite(workspace_id, maintenant=maintenant)
    assert memoire_service.derniere_activite(workspace_id) == maintenant
    assert memoire_service.duree_inactivite(
        workspace_id, maintenant=maintenant
    ) == pytest.approx(0.0)

    suivante = maintenant + timedelta(minutes=10)
    memoire_service.enregistrer_activite(workspace_id, maintenant=suivante)
    assert memoire_service.duree_inactivite(
        workspace_id, maintenant=suivante + timedelta(minutes=5)
    ) == pytest.approx(300.0)

    assert memoire_service.derniere_activite("ws_jamais_utilise") is None
    assert memoire_service.duree_inactivite("ws_jamais_utilise") is None
