"""Surface test for multi-ref orchestration (pas de GPU)."""

from __future__ import annotations

import asyncio
import io

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from PIL import Image

from backend.db.models import Base
from backend.services import image_service as svc
from backend.services import flux_spike as fx


def _mini_png(size=(4, 4)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (1, 2, 3)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture()
def tmp_repo(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    monkeypatch.setattr("backend.db.database.SessionLocal", TestSession)
    monkeypatch.setenv("THOT_IMAGE_OPTIN_PATH", str(tmp_path / "image_backend.optin"))
    monkeypatch.delenv("THOT_IMAGE_BACKEND", raising=False)
    data_dir = tmp_path / "data"
    (data_dir / "blobs" / "ab").mkdir(parents=True)
    monkeypatch.setattr("backend.config.paths.get_data_dir", lambda: data_dir)
    monkeypatch.setattr(svc, "check_install", lambda: (True, "ok"))
    monkeypatch.setattr(svc, "check_vram", lambda: (True, "ok"))
    monkeypatch.setattr(svc, "_rendre_la_vram_du_worker_ocr", lambda: "ok")
    monkeypatch.setattr(
        svc, "_resolve_work_token",
        lambda token, supplied_sha256=None: (b"ref", 1024, 1024, "sha"),
    )
    return tmp_path, data_dir, TestSession


def test_multi_ref_mock_invokes_reference_images(tmp_repo, monkeypatch):
    _, _, TestSession = tmp_repo
    captured = {}

    def fake_run_t2i(*a, **k):
        captured["args"] = a
        captured["kwargs"] = k
        return _mini_png((1024, 1024)), {
            "seed": 7,
            "width": 1024,
            "height": 1024,
            "backend": fx.BACKEND_NAME,
        }

    monkeypatch.setattr(fx, "run_t2i", fake_run_t2i)

    result = svc.generate(
        None,
        "run-multi",
        "prompt exact",
        "1:1",
        reference_tokens=["0" * 32, "1" * 32],
        seed=7,
        db_session_factory=TestSession,
        origin="studio",
    )
    assert result is not None
    assert len(captured["kwargs"].get("reference_images") or []) == 2
    assert all(isinstance(p, str) for p in captured["kwargs"]["reference_images"])
