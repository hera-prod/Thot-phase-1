"""La réservation globale du travail lourd et les événements d'entretien.

Le verrou binaire couvre chat, image et mémoire. Ces tests vérifient l'atomicité
du verrou, la libération idempotente, la génération qui protège une réservation
périmée, puis le contrat d'événements du wrapper : différé une seule fois,
démarré puis terminé sur le vrai chemin, échec sur exception.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import main as backend_main
from backend.db import database as database_module
from backend.db.models import Base, Conversation, Message, Run, Workspace
from backend.ipc import espaces as IPC
from backend.services import activity
from backend.services import memoire_auto as A


@pytest.fixture(autouse=True)
def _verrou_propre():
    """Aucun test ne doit hériter d'un verrou ou d'un état d'événement."""
    A._DERNIER_ETAT.clear()
    yield
    with activity._OWNER_LOCK:
        activity._OWNER = None
    activity.TURN_ACTIVE.clear()
    try:
        activity._RESERVATION_LOCK.release()
    except RuntimeError:
        pass
    A._DERNIER_ETAT.clear()


@pytest.fixture()
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'memoire_reservation.db'}")
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


@pytest.fixture()
def events(monkeypatch):
    capture: list[tuple[str, dict]] = []

    def fake_emit(event, payload, **kwargs):
        capture.append((event, payload))
        return True

    monkeypatch.setattr("backend.inference.ipc.emit", fake_emit)
    return capture


def _espace(
    db,
    workspace_id: str = "ws_res",
    *,
    tours: int = 5,
    activite: datetime | None = None,
) -> str:
    db.add(
        Workspace(
            id=workspace_id,
            name=workspace_id,
            state="active",
            auto_memory_turn_count=tours,
            last_activity_at=activite,
            updated_at=activite,
        )
    )
    db.add(
        Conversation(
            id=f"conv_{workspace_id}", title="Projet", workspace_id=workspace_id
        )
    )
    db.commit()
    return workspace_id


def _message(db, workspace_id: str, contenu: str) -> None:
    db.add(
        Message(
            id=f"msg_{workspace_id}",
            conv_id=f"conv_{workspace_id}",
            role="user",
            content=contenu,
            created_at=datetime(2026, 10, 25, 12, 0, 0),
        )
    )
    db.commit()


# ---------- 1-3. Le verrou ----------


def test_chat_bloque_memoire_puis_la_liberation_la_laisse_passer():
    chat = activity.try_reserver("chat", "chat:1")
    assert chat is not None
    assert activity.try_reserver("memoire", "memoire:ws") is None

    activity.liberer(chat)
    memoire = activity.try_reserver("memoire", "memoire:ws")
    assert memoire is not None
    activity.liberer(memoire)


def test_memoire_bloque_chat_et_image_sans_poser_turn_active():
    memoire = activity.try_reserver("memoire", "memoire:ws")
    assert memoire is not None
    assert activity.TURN_ACTIVE.is_set() is False
    assert activity.try_reserver("chat", "chat:1") is None
    assert activity.try_reserver("image", "image:1") is None
    activity.liberer(memoire)


def test_liberer_deux_fois_est_idempotent_et_une_reservation_perimee_ne_libere_pas():
    premier = activity.try_reserver("memoire", "memoire:ws")
    assert premier is not None
    activity.liberer(premier)
    activity.liberer(premier)  # idempotent

    second = activity.try_reserver("memoire", "memoire:ws")
    assert second is not None
    activity.liberer(premier)  # périmée : ne doit pas libérer la nouvelle
    assert activity.etat_activite()["owner_id"] == "memoire:ws"
    assert activity.try_reserver("chat", "chat:1") is None

    activity.liberer(second)
    chat = activity.try_reserver("chat", "chat:1")
    assert chat is not None
    activity.liberer(chat)


# ---------- 4-6. Le wrapper d'entretien ----------


def test_wrapper_differe_sans_started_quand_le_chat_tient_le_verrou(db, events):
    maintenant = datetime(2026, 10, 25, 12, 0, 0)
    workspace_id = _espace(
        db,
        "ws_differe",
        activite=maintenant - timedelta(minutes=6),
    )
    chat = activity.try_reserver("chat", "chat:1")
    try:
        out = A.executer_entretien_memoire(db, workspace_id, mode="auto")
    finally:
        activity.liberer(chat)

    assert out == {
        "ok": True,
        "statut": "differe",
        "raison": "verrou_occupe",
        "bloque_par": "chat",
    }
    assert [event for event, _ in events] == ["memory_deferred"]
    assert events[0][1]["statut"] == "differe"
    assert events[0][1]["bloque_par"] == "chat"


def test_differe_reemet_apres_heartbeat_ou_changement_de_bloqueur(
    db, events, monkeypatch
):
    maintenant = datetime(2026, 10, 25, 12, 0, 0)
    workspace_id = _espace(
        db,
        "ws_heartbeat",
        activite=maintenant - timedelta(minutes=6),
    )
    horloge = {"t": 1000.0}
    monkeypatch.setattr(A.time, "monotonic", lambda: horloge["t"])

    chat = activity.try_reserver("chat", "chat:heartbeat")
    assert chat is not None
    try:
        A.executer_entretien_memoire(
            db, workspace_id, mode="auto", maintenant=maintenant
        )
        assert [nom for nom, _ in events] == ["memory_deferred"]

        # Immédiat et même bloqueur : le heartbeat n'est pas du bruit.
        A.executer_entretien_memoire(
            db, workspace_id, mode="auto", maintenant=maintenant
        )
        assert len(events) == 1

        # Plus de 60 s sans nouvel événement : le renderer effacerait le différé
        # à 120 s, donc le service le réémet.
        horloge["t"] += A.MEMORY_DEFERRED_HEARTBEAT_S + 1
        A.executer_entretien_memoire(
            db, workspace_id, mode="auto", maintenant=maintenant
        )
        assert len(events) == 2
        assert events[1][1]["bloque_par"] == "chat"

        # Le bloqueur change (chat → image) : nouvel événement même avant 60 s.
        activity.liberer(chat)
        chat = None
        image = activity.try_reserver("image", "image:heartbeat")
        assert image is not None
        try:
            A.executer_entretien_memoire(
                db, workspace_id, mode="auto", maintenant=maintenant
            )
            assert len(events) == 3
            assert events[2][1]["bloque_par"] == "image"
        finally:
            activity.liberer(image)
    finally:
        if chat is not None:
            activity.liberer(chat)


def test_wrapper_emet_started_puis_finished_et_libere_le_verrou(db, events):
    maintenant = datetime(2026, 10, 25, 12, 10, 0)
    workspace_id = _espace(
        db,
        "ws_effectue",
        activite=maintenant - timedelta(minutes=6),
    )
    _message(db, workspace_id, "On utilise SQLite comme base de données.")

    out = A.executer_entretien_memoire(
        db, workspace_id, mode="auto", maintenant=maintenant
    )

    assert out["statut"] == "effectue"
    assert [event for event, _ in events] == ["memory_started", "memory_finished"]
    assert events[1][1]["statut"] == "effectue"
    chat = activity.try_reserver("chat", "chat:libre")
    assert chat is not None
    activity.liberer(chat)


def test_wrapper_emet_finished_echec_puis_relance_et_libere(db, events, monkeypatch):
    workspace_id = _espace(db, "ws_echec")

    def entretenir_qui_echoue(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(A, "entretenir_memoire", entretenir_qui_echoue)

    with pytest.raises(RuntimeError, match="boom"):
        A.executer_entretien_memoire(db, workspace_id, force=True, mode="force")

    assert [event for event, _ in events] == ["memory_started", "memory_finished"]
    fin = events[1][1]
    assert fin["statut"] == "echec"
    assert fin["code"] == "MEMORY_MAINTENANCE_FAILED"
    assert fin["message"] == "boom"
    libre = activity.try_reserver("chat", "chat:libre")
    assert libre is not None
    activity.liberer(libre)


# ---------- 7. L'état exposé à l'écran ----------


def test_espace_memoire_expose_entretien_et_l_etat_du_verrou(db):
    maintenant = datetime(2026, 10, 25, 12, 0, 0)
    workspace_id = _espace(
        db,
        "ws_ipc",
        activite=maintenant - timedelta(minutes=6),
    )

    payload = IPC.espace_memoire(db, {"workspaceId": workspace_id})
    entretien = payload["entretien"]
    assert entretien["tours"] == 5
    assert entretien["seuil_tours"] == A.SEUIL_TOURS_MEMOIRE
    assert entretien["etat"] == "en_attente"
    assert entretien["raison"] == "seuil_atteint"

    chat = activity.try_reserver("chat", "chat:1")
    try:
        differe = IPC.espace_memoire(db, {"workspaceId": workspace_id})["entretien"]
    finally:
        activity.liberer(chat)
    assert differe["etat"] == "differe"
    assert differe["raison"] == "tour_actif"

    memoire = activity.try_reserver("memoire", f"memoire:{workspace_id}")
    try:
        en_cours = IPC.espace_memoire(db, {"workspaceId": workspace_id})["entretien"]
    finally:
        activity.liberer(memoire)
    assert en_cours["etat"] == "en_cours"
    assert en_cours["raison"] == "en_cours"


# ---------- 8. Atomicité multi-thread ----------


def test_barriere_multi_thread_un_seul_obtient_la_reservation():
    """Deux prétendants simultanés : le verrou n'a qu'un gagnant."""
    barriere = threading.Barrier(2)
    resultats: dict[str, activity.Reservation | None] = {}

    def tenter(kind: str, owner: str) -> None:
        barriere.wait()
        resultats[kind] = activity.try_reserver(kind, owner)

    threads = [
        threading.Thread(target=tenter, args=("chat", "chat:barriere")),
        threading.Thread(target=tenter, args=("memoire", "memoire:barriere")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    gagnants = [res for res in resultats.values() if res is not None]
    assert len(gagnants) == 1
    assert sum(res is None for res in resultats.values()) == 1
    activity.liberer(gagnants[0])
    assert activity.try_reserver("chat", "chat:apres") is not None


# ---------- 9. Propriétaire et génération ----------


def test_mauvais_proprietaire_ne_libere_pas_la_reservation_reelle():
    reelle = activity.try_reserver("memoire", "memoire:ws")
    assert reelle is not None

    mauvais_owner = activity.Reservation("memoire", "memoire:autre", reelle.generation)
    activity.liberer(mauvais_owner)
    assert activity.etat_activite()["owner_id"] == "memoire:ws"

    mauvaise_generation = activity.Reservation(
        "memoire", "memoire:ws", reelle.generation + 1
    )
    activity.liberer(mauvaise_generation)
    assert activity.etat_activite()["owner_id"] == "memoire:ws"
    assert activity.try_reserver("chat", "chat:bloque") is None

    activity.liberer(reelle)
    assert activity.try_reserver("chat", "chat:libre") is not None


def test_double_liberation_est_idempotente_et_ne_touche_pas_une_autre_reservation():
    premiere = activity.try_reserver("chat", "chat:double")
    assert premiere is not None
    activity.liberer(premiere)
    activity.liberer(premiere)

    suivante = activity.try_reserver("memoire", "memoire:double")
    assert suivante is not None
    activity.liberer(premiere)  # périmée : ne doit pas libérer `suivante`
    assert activity.etat_activite()["owner_id"] == "memoire:double"
    assert activity.try_reserver("chat", "chat:encore") is None

    activity.liberer(suivante)
    assert activity.try_reserver("chat", "chat:encore") is not None


# ---------- 10. Branche chat : ordre de libération et refus ----------


@pytest.fixture()
def chat_main(tmp_path, monkeypatch):
    """Vraie branche `chat.send` : base temporaire, boucle sans modèle."""
    engine = create_engine(f"sqlite:///{tmp_path / 'chat_main.db'}")
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    monkeypatch.setattr(database_module, "SessionLocal", TestSession)
    monkeypatch.setattr(backend_main, "manager", None)
    monkeypatch.setattr(backend_main, "get_chat_runtime", lambda: None)
    monkeypatch.setattr(
        "backend.inference.loop.run_agent_loop",
        lambda manager, **kwargs: ("réponse", {"answer_emitted": True}),
    )
    emitted: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        backend_main,
        "emit",
        lambda event, payload, **kwargs: emitted.append((event, payload)),
    )
    return TestSession, emitted


def _chat_request(request_id: str, message: str = "bonjour") -> dict:
    return {
        "request_id": request_id,
        "method": "chat.send",
        "params": {"message": message, "conv_id": request_id},
        "protocol_version": 1,
    }


def test_chat_garde_la_reservation_jusqu_apres_le_terminal(chat_main, monkeypatch):
    sessions, emitted = chat_main
    etats: dict[str, str | None] = {}

    def emit_qui_observe(event, payload, **kwargs):
        emitted.append((event, payload))
        etats[event] = activity.etat_activite()["kind"]

    monkeypatch.setattr(backend_main, "emit", emit_qui_observe)

    pendant_finaliser: dict[str, str | None] = {}

    def faux_finaliser(db, conversation_id, workspace_id=None):
        pendant_finaliser["kind"] = activity.etat_activite()["kind"]
        return {"ok": True, "statut": "tour_compte"}

    monkeypatch.setattr(A, "finaliser_tour_reussi", faux_finaliser)

    backend_main.handle(_chat_request("chat-ordre-liberation"))

    assert pendant_finaliser["kind"] == "chat"
    assert etats["run_completed"] == "chat"
    assert activity.etat_activite()["kind"] is None

    db = sessions()
    try:
        assert (
            db.query(Message).filter(Message.run_id == "chat-ordre-liberation").count()
            == 2
        )
    finally:
        db.close()


def test_refus_chat_memory_busy_n_ecrit_ni_message_ni_run(chat_main):
    sessions, emitted = chat_main
    memoire = activity.try_reserver("memoire", "memoire:refus-chat")
    assert memoire is not None
    try:
        backend_main.handle(_chat_request("chat-refuse"))
        # La réservation mémoire est toujours celle du test, pas celle du chat refusé.
        assert activity.etat_activite()["owner_id"] == "memoire:refus-chat"
    finally:
        activity.liberer(memoire)

    erreurs = [payload for nom, payload in emitted if nom == "error"]
    assert [payload["code"] for payload in erreurs] == ["MEMORY_BUSY"]
    assert erreurs[0]["request_id"] == "chat-refuse"
    assert [nom for nom, _ in emitted if nom == "run_started"] == []

    db = sessions()
    try:
        assert db.query(Message).count() == 0
        assert db.query(Run).count() == 0
    finally:
        db.close()


# ---------- 11. Mémoire différée : le chemin passe par le wrapper ----------


def test_memoire_differee_n_emet_pas_started(db, events):
    maintenant = datetime(2026, 10, 25, 12, 0, 0)
    workspace_id = _espace(
        db,
        "ws_differe_sans_started",
        activite=maintenant - timedelta(minutes=6),
    )
    chat = activity.try_reserver("chat", "chat:differe")
    try:
        out = A.executer_entretien_memoire(
            db, workspace_id, mode="auto", maintenant=maintenant
        )
    finally:
        activity.liberer(chat)

    assert out["statut"] == "differe"
    noms = [nom for nom, _ in events]
    assert noms == ["memory_deferred"]
    assert "memory_started" not in noms


# ---------- 12. Branche image : refus sans création de run ----------


def test_refus_image_memory_busy_ne_cree_aucun_run(chat_main):
    sessions, emitted = chat_main
    memoire = activity.try_reserver("memoire", "memoire:refus-image")
    assert memoire is not None
    try:
        backend_main.handle(
            {
                "request_id": "image-refuse",
                "method": "image.generate",
                "params": {"prompt": "un phare"},
                "protocol_version": 1,
            }
        )
    finally:
        activity.liberer(memoire)

    erreurs = [payload for nom, payload in emitted if nom == "error"]
    echecs = [payload for nom, payload in emitted if nom == "run_failed"]
    assert [payload["code"] for payload in erreurs] == ["MEMORY_BUSY"]
    assert [payload["code"] for payload in echecs] == ["MEMORY_BUSY"]
    assert [nom for nom, _ in emitted if nom == "run_started"] == []

    db = sessions()
    try:
        assert db.query(Run).count() == 0
    finally:
        db.close()
