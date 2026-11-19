"""Le rattrapage mémoire après un vrai redémarrage du sidecar.

Les compteurs d'un espace (`auto_memory_turn_count`, `last_activity_at`,
`memory_last_processed_at`) sont DURABLES : ils survivent à la fermeture de la
session et au redémarrage. Le drapeau `running` de l'indicateur, lui, n'est
JAMAIS durable : il est redérivé des événements du nouveau processus. Un espace
dû au moment de la coupure doit donc être entretenu à la relance, sans état
volatil à restaurer.
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
from backend.services import activity
from backend.services import memoire_auto as A
from backend.services import memoire_scheduler as S


@pytest.fixture(autouse=True)
def _verrou_propre():
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
def session_factory(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'memoire_redemarrage.db'}")
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    monkeypatch.setattr("backend.db.database.SessionLocal", TestSession)
    yield TestSession


@pytest.fixture()
def events(monkeypatch):
    capture: list[tuple[str, dict]] = []

    def fake_emit(event, payload, **kwargs):
        capture.append((event, payload))
        return True

    monkeypatch.setattr("backend.inference.ipc.emit", fake_emit)
    return capture


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _peupler_espace_du(db, workspace_id: str) -> None:
    """Espace dû : cinq tours, six minutes d'inactivité, cinq messages."""
    activite = _now() - timedelta(minutes=6)
    db.add(
        Workspace(
            id=workspace_id,
            name=workspace_id,
            state="active",
            auto_memory_turn_count=5,
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
    for index in range(5):
        db.add(
            Message(
                id=f"msg_{workspace_id}_{index}",
                conv_id=f"conv_{workspace_id}",
                role="user",
                content="On utilise SQLite pour la base de données.",
                created_at=_now() - timedelta(minutes=10) + timedelta(seconds=index),
            )
        )
    db.commit()


def test_relance_apres_fermeture_entretient_l_espace_du(session_factory, events):
    workspace_id = "ws_redemarre"
    db = session_factory()
    _peupler_espace_du(db, workspace_id)
    db.close()  # fermeture : plus aucun état de session ne relie la relance

    # `_entretenir_un_espace` ouvre lui-même une NOUVELLE session.
    S._entretenir_un_espace(workspace_id)

    db = session_factory()
    try:
        ws = db.query(Workspace).filter(Workspace.id == workspace_id).first()
        assert ws.auto_memory_turn_count == 0
        assert ws.memory_last_processed_at is not None
        assert (
            db.query(MemoryEntry)
            .filter(
                MemoryEntry.workspace_id == workspace_id,
                MemoryEntry.is_current == 1,
            )
            .count()
            >= 1
        )
        assert (
            db.query(MemorySynthesis)
            .filter(
                MemorySynthesis.workspace_id == workspace_id,
                MemorySynthesis.is_current == 1,
            )
            .count()
            == 1
        )
    finally:
        db.close()

    noms = [event for event, _ in events]
    assert noms == ["memory_started", "memory_finished"]
    assert events[1][1]["statut"] == "effectue"


def test_reservation_chat_bloque_la_relance_puis_l_entretien_se_fait(
    session_factory, events
):
    workspace_id = "ws_redemarre_occupe"
    db = session_factory()
    _peupler_espace_du(db, workspace_id)
    db.close()

    chat = activity.try_reserver("chat", "chat:relance")
    try:
        S._entretenir_un_espace(workspace_id)
        assert [event for event, _ in events] == ["memory_deferred"]
        assert events[0][1]["bloque_par"] == "chat"

        db = session_factory()
        try:
            ws = db.query(Workspace).filter(Workspace.id == workspace_id).first()
            assert ws.auto_memory_turn_count == 5
            assert ws.memory_last_processed_at is None
        finally:
            db.close()
    finally:
        activity.liberer(chat)

    events.clear()
    S._entretenir_un_espace(workspace_id)

    assert [event for event, _ in events] == ["memory_started", "memory_finished"]
    assert events[1][1]["statut"] == "effectue"

    db = session_factory()
    try:
        ws = db.query(Workspace).filter(Workspace.id == workspace_id).first()
        assert ws.auto_memory_turn_count == 0
        assert ws.memory_last_processed_at is not None
    finally:
        db.close()
