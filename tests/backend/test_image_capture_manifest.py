"""Tests du manifeste de capture d'image."""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.db.models import Base
from backend import services
from backend.services import image_service as svc
from backend.services import flux_spike as fx


def _png(size=(1024, 1024), mode="RGB", color=(5, 6, 7)):
    img = Image.new(mode, size, color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _mask(size=(1024, 1024)):
    img = Image.new("L", size, 0)
    for y in range(100, 200):
        for x in range(100, 200):
            img.putpixel((x, y), 255)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
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
    (tmp_path / "image_backend.optin").write_text("flux2-spike")
    monkeypatch.delenv("THOT_IMAGE_BACKEND", raising=False)
    data_dir = tmp_path / "data"
    (data_dir / "blobs" / "ab").mkdir(parents=True)
    monkeypatch.setattr("backend.config.paths.get_data_dir", lambda: data_dir)
    monkeypatch.setattr(svc, "check_install", lambda: (True, "ok"))
    monkeypatch.setattr(svc, "check_vram", lambda: (True, "ok"))
    monkeypatch.setattr(svc, "_rendre_la_vram_du_worker_ocr", lambda: "ok")
    return tmp_path, data_dir, TestSession


def _setup_resolver(monkeypatch, work_png, digest=None):
    if digest is None:
        digest = hashlib.sha256(work_png).hexdigest()

    def resolver(token, supplied_sha256=None):
        return (
            work_png,
            1024,
            1024,
            supplied_sha256 if supplied_sha256 is not None else digest,
        )

    monkeypatch.setattr(svc, "_resolve_work_token", resolver)


def test_hash_recomputed(tmp_repo, monkeypatch):
    tmp_path, data_dir, TestSession = tmp_repo
    work_png = _png(color=(1, 2, 3))
    mask_png = _mask()
    capture_dir = tmp_path / "captures"
    monkeypatch.setenv("THOT_IMAGE_CAPTURE_DIR", str(capture_dir))

    # resolver returns real digest (no mismatch)
    # mask digest too
    def resolver(token, supplied_sha256=None):
        if str(token).strip() == "1" * 32:
            return (mask_png, 1024, 1024, hashlib.sha256(mask_png).hexdigest())
        return (work_png, 1024, 1024, hashlib.sha256(work_png).hexdigest())

    monkeypatch.setattr(svc, "_resolve_work_token", resolver)

    called = {}

    def fake_run_edit(*a, **k):
        called["n"] = called.get("n", 0) + 1
        buf = io.BytesIO()
        Image.new("RGB", (1024, 1024), (9, 9, 9)).save(buf, format="PNG")
        return buf.getvalue(), {
            "steps_executed": 4,
            "seed": k.get("seed", 7),
            "width": 1024,
            "height": 1024,
            "backend": fx.BACKEND_NAME,
        }

    monkeypatch.setattr(fx, "run_edit", fake_run_edit)

    svc.generate(
        None,
        "run-complete",
        "prompt",
        "1:1",
        mode="inpaint",
        work_token="0" * 32,
        mask_token="1" * 32,
        mask_kind="fill",
        seed=7,
        canvas_width=1024,
        canvas_height=1024,
        source_transform={"x": 100, "y": 100, "width": 100, "height": 100},
        source_size={"width": 200, "height": 200},
        origin="model_inference",
        db_session_factory=TestSession,
    )

    run_dir = capture_dir / "run-complete"
    manifest = json.loads((run_dir / "manifest.json").read_text())
    for name, info in manifest["files"].items():
        assert (
            info["sha256"] == hashlib.sha256((run_dir / name).read_bytes()).hexdigest()
        )
    assert manifest["capture_status"] == "complete"
    assert manifest["origin"] == "model_inference"
    assert manifest["inference"]["steps_executed"] == 4


def test_invalid_hash_mismatch(tmp_repo, monkeypatch):
    tmp_path, data_dir, TestSession = tmp_repo
    work_png = _png(color=(1, 2, 3))
    mask_png = _mask()
    capture_dir = tmp_path / "captures2"
    monkeypatch.setenv("THOT_IMAGE_CAPTURE_DIR", str(capture_dir))

    def resolver(token, supplied_sha256=None):
        # returns "sha" digest -> mismatch
        if str(token).strip() == "1" * 32:
            return (mask_png, 1024, 1024, "sha")
        return (work_png, 1024, 1024, "sha")

    monkeypatch.setattr(svc, "_resolve_work_token", resolver)

    def fake_run_edit(*a, **k):
        buf = io.BytesIO()
        Image.new("RGB", (1024, 1024), (9, 9, 9)).save(buf, format="PNG")
        return buf.getvalue(), {
            "steps_executed": 4,
            "seed": 7,
            "width": 1024,
            "height": 1024,
            "backend": fx.BACKEND_NAME,
        }

    monkeypatch.setattr(fx, "run_edit", fake_run_edit)

    svc.generate(
        None,
        "run-invalid",
        "prompt",
        "1:1",
        mode="inpaint",
        work_token="0" * 32,
        mask_token="1" * 32,
        mask_kind="fill",
        seed=7,
        canvas_width=1024,
        canvas_height=1024,
        source_transform={"x": 1, "y": 1, "width": 10, "height": 10},
        source_size={"width": 200, "height": 200},
        origin="model_inference",
        db_session_factory=TestSession,
    )

    run_dir = capture_dir / "run-invalid"
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["capture_status"] == "invalid"
    assert manifest["hash_mismatch"]  # non-empty


def test_synthetic_steps_unreported(tmp_repo, monkeypatch):
    tmp_path, data_dir, TestSession = tmp_repo
    work_png = _png(color=(1, 2, 3))
    mask_png = _mask()
    capture_dir = tmp_path / "captures3"
    monkeypatch.setenv("THOT_IMAGE_CAPTURE_DIR", str(capture_dir))

    def resolver(token, supplied_sha256=None):
        if str(token).strip() == "1" * 32:
            return (mask_png, 1024, 1024, hashlib.sha256(mask_png).hexdigest())
        return (work_png, 1024, 1024, hashlib.sha256(work_png).hexdigest())

    monkeypatch.setattr(svc, "_resolve_work_token", resolver)

    def fake_run_edit(*a, **k):
        buf = io.BytesIO()
        Image.new("RGB", (1024, 1024), (9, 9, 9)).save(buf, format="PNG")
        return buf.getvalue(), {
            "steps_executed": None,
            "seed": 7,
            "width": 1024,
            "height": 1024,
            "backend": fx.BACKEND_NAME,
        }

    monkeypatch.setattr(fx, "run_edit", fake_run_edit)

    svc.generate(
        None,
        "run-sentinel",
        "prompt",
        "1:1",
        mode="inpaint",
        work_token="0" * 32,
        mask_token="1" * 32,
        mask_kind="fill",
        seed=7,
        canvas_width=1024,
        canvas_height=1024,
        source_transform={"x": 1, "y": 1, "width": 10, "height": 10},
        source_size={"width": 200, "height": 200},
        origin="synthetic_sentinel",
        db_session_factory=TestSession,
    )

    run_dir = capture_dir / "run-sentinel"
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["capture_status"] == "incomplete"
    assert manifest["capture_reason"] == "steps_unreported"


def test_missing_source_transform(tmp_repo, monkeypatch):
    tmp_path, data_dir, TestSession = tmp_repo
    work_png = _png(color=(1, 2, 3))
    mask_png = _mask()
    capture_dir = tmp_path / "captures4"
    monkeypatch.setenv("THOT_IMAGE_CAPTURE_DIR", str(capture_dir))

    def resolver(token, supplied_sha256=None):
        if str(token).strip() == "1" * 32:
            return (mask_png, 1024, 1024, hashlib.sha256(mask_png).hexdigest())
        return (work_png, 1024, 1024, hashlib.sha256(work_png).hexdigest())

    monkeypatch.setattr(svc, "_resolve_work_token", resolver)

    def fake_run_edit(*a, **k):
        buf = io.BytesIO()
        Image.new("RGB", (1024, 1024), (9, 9, 9)).save(buf, format="PNG")
        return buf.getvalue(), {
            "steps_executed": 4,
            "seed": 7,
            "width": 1024,
            "height": 1024,
            "backend": fx.BACKEND_NAME,
        }

    monkeypatch.setattr(fx, "run_edit", fake_run_edit)

    svc.generate(
        None,
        "run-notransform",
        "prompt",
        "1:1",
        mode="inpaint",
        work_token="0" * 32,
        mask_token="1" * 32,
        mask_kind="fill",
        seed=7,
        canvas_width=1024,
        canvas_height=1024,
        source_transform=None,
        source_size={"width": 200, "height": 200},
        origin="model_inference",
        db_session_factory=TestSession,
    )

    run_dir = capture_dir / "run-notransform"
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["capture_status"] == "incomplete"
    assert manifest["capture_reason"] == "missing_source_transform"
