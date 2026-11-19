from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.db.models import Base, Conversation, Run
from backend.inference.loop import _ensure_chat_run


def _creer_conversation(session, conv_id: str) -> None:
    '''La destination existe : ces tests verrouillent le snapshot de runtime,
    pas la re-vérification de discussion (orchestration.md §2.1, qui exige les
    deux au passage à l'exécution).'''
    db = session()
    db.add(Conversation(id=conv_id, title="essai", mode="chat"))
    db.commit()
    db.close()


def test_runtime_snapshot_is_frozen(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr("backend.db.database.SessionLocal", session)
    _creer_conversation(session, "conv_1")
    _ensure_chat_run("run_1", "conv_1", "llama_cpp", "llama_cpp:LFM")
    with pytest.raises(RuntimeError, match="RUNTIME_MISMATCH"):
        _ensure_chat_run("run_1", "conv_1", "llama_server", "llama_server:other")


def test_missing_legacy_snapshot_is_rejected(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr("backend.db.database.SessionLocal", session)
    db = session()
    db.add(Run(id="legacy", request_id="legacy", kind="chat", status="running"))
    db.commit()
    db.close()
    _creer_conversation(session, "conv_legacy")
    with pytest.raises(RuntimeError, match="missing_snapshot"):
        _ensure_chat_run("legacy", "conv_legacy")


def test_snapshot_failure_is_not_swallowed(monkeypatch) -> None:
    class BrokenSession:
        def query(self, *_args):
            raise RuntimeError("db down")

        def rollback(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr("backend.db.database.SessionLocal", lambda: BrokenSession())
    with pytest.raises(RuntimeError, match="db down"):
        _ensure_chat_run("run_2", "conv_2")
