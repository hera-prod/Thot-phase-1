"""La discussion créée appartient RÉELLEMENT à l'espace demandé (chat.send).

Le bouton « Nouvelle discussion » d'une page Espace promet une discussion dans
cet espace. Cette promesse n'est vraie que si l'espace atteint la création de la
conversation : un identifiant d'espace refusé, ou un espace en corbeille accepté,
produirait une discussion hors espace — invisible depuis la page qui l'a créée.
Sans modèle : base temporaire, boucle d'agent remplacée par un stub.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import main as backend_main
from backend.db import database as database_module
from backend.db.models import Base, Conversation
from backend.services import espaces as service


@pytest.fixture()
def espace(tmp_path, monkeypatch):
    """Base temporaire, `SessionLocal` du projet, boucle sans modèle, émissions capturées."""
    engine = create_engine(f"sqlite:///{tmp_path / 'conv-espace.db'}")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    monkeypatch.setattr(database_module, "SessionLocal", session_factory)
    monkeypatch.setattr(backend_main, "manager", None)
    monkeypatch.setattr(backend_main, "get_chat_runtime", lambda: None)
    monkeypatch.setattr(
        "backend.inference.loop.run_agent_loop",
        lambda manager, **kwargs: ("réponse", {"answer_emitted": True}),
    )
    emitted: list[tuple[str, dict]] = []
    monkeypatch.setattr(backend_main, "emit", lambda event, payload, **kwargs: emitted.append((event, payload)))
    return session_factory, emitted


def _chat(request_id: str, params: dict) -> dict:
    return {
        "request_id": request_id,
        "method": "chat.send",
        "params": {"message": "bonjour", "conv_id": request_id, **params},
        "protocol_version": 1,
    }


def _evenements(emitted: list[tuple[str, dict]], nom: str) -> list[dict]:
    return [payload for name, payload in emitted if name == nom]


def test_discussion_creee_appartient_a_l_espace_demande(espace):
    sessions, emitted = espace
    db = sessions()
    try:
        workspace = service.create_workspace(db, "Recherche")
        workspace_id = str(workspace.id)
    finally:
        db.close()

    backend_main.handle(_chat("avec-espace", {"workspace_id": workspace_id}))

    db = sessions()
    try:
        conv = db.query(Conversation).filter(Conversation.id == "avec-espace").one()
        assert conv.workspace_id == workspace_id
    finally:
        db.close()
    assert not _evenements(emitted, "error")


def test_camelCase_est_accepte_comme_space_depuis_le_frontend(espace):
    sessions, _ = espace
    db = sessions()
    try:
        workspace_id = str(service.create_workspace(db, "Notes").id)
    finally:
        db.close()

    backend_main.handle(_chat("camel-espace", {"workspaceId": workspace_id}))

    db = sessions()
    try:
        assert db.query(Conversation).filter(Conversation.id == "camel-espace").one().workspace_id == workspace_id
    finally:
        db.close()


def test_espace_en_corbeille_refuse_et_ne_cree_rien(espace):
    sessions, emitted = espace
    db = sessions()
    try:
        workspace_id = str(service.create_workspace(db, "Vieux").id)
        service.trash_workspace(db, workspace_id)
    finally:
        db.close()

    backend_main.handle(_chat("espace-corbeille", {"workspace_id": workspace_id}))

    erreurs = _evenements(emitted, "error")
    assert erreurs and erreurs[0]["code"] == "workspace_not_found"
    assert erreurs[0]["retryable"] is False
    db = sessions()
    try:
        assert db.query(Conversation).filter(Conversation.id == "espace-corbeille").first() is None
    finally:
        db.close()


def test_espace_inconnu_refuse_et_ne_cree_rien(espace):
    sessions, emitted = espace

    backend_main.handle(_chat("espace-fantome", {"workspace_id": "ws_inexistant"}))

    erreurs = _evenements(emitted, "error")
    assert erreurs and erreurs[0]["code"] == "workspace_not_found"
    db = sessions()
    try:
        assert db.query(Conversation).filter(Conversation.id == "espace-fantome").first() is None
    finally:
        db.close()


def test_sans_espace_la_conversation_reste_un_chat_simple(espace):
    sessions, emitted = espace

    backend_main.handle(_chat("hors-espace", {}))

    db = sessions()
    try:
        conv = db.query(Conversation).filter(Conversation.id == "hors-espace").one()
        assert conv.workspace_id is None
    finally:
        db.close()
    assert not _evenements(emitted, "error")


def test_espace_vide_est_ignore_comme_absent(espace):
    sessions, emitted = espace

    backend_main.handle(_chat("espace-vide", {"workspace_id": "   "}))

    db = sessions()
    try:
        assert db.query(Conversation).filter(Conversation.id == "espace-vide").one().workspace_id is None
    finally:
        db.close()
    assert not _evenements(emitted, "error")
