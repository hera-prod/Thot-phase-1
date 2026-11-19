"""Tests ciblés pour le fallback de prompt vide (inpaint/outpaint)."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from backend.services import image_service as svc
from backend.services import flux_spike as fx


FALLBACK = (
    "Reconstruct and extend naturally the masked area matching the surrounding scene."
)


def test_validate_request_empty_allowed_inpaint_outpaint():
    for mode in ("inpaint", "outpaint", "  INPAINT  ", "OutPaint"):
        p, f, refs, seed = svc.validate_request("", "1:1", [], mode=mode)
        assert p == FALLBACK
        assert f == "1:1"
        assert refs == []
        assert isinstance(seed, int)


def test_validate_request_empty_rejected_other_modes():
    for mode in (None, "create", "reference", "global", "local", "unknown"):
        with pytest.raises(ValueError, match="EMPTY_QUERY"):
            svc.validate_request("", "1:1", [], mode=mode)


def test_validate_request_empty_positional_or_keyword_mode():
    # mode after mask_kind: keyword works; empty without mode rejected
    with pytest.raises(ValueError, match="EMPTY_QUERY"):
        svc.validate_request("   ", "1:1", [])
    p, *_ = svc.validate_request("   ", "1:1", [], mask_kind="fill", mode="inpaint")
    assert p == FALLBACK


def test_validate_request_max_length_still_enforced():
    long_prompt = "x" * (svc.PROMPT_MAX_CHARS + 1)
    with pytest.raises(ValueError, match="TOO_LONG"):
        svc.validate_request(long_prompt, "1:1", [], mode="inpaint")


# ---------- generate() ----------


@pytest.fixture()
def tmp_repo(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from backend.db.models import Base

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
    return tmp_path, data_dir, TestSession


def _mask_png(size=(1024, 1024), value=200):
    img = Image.new("L", size, 0)
    img.putpixel((10, 10), value)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _mock(svc, fx, monkeypatch, mask_png, capture):
    monkeypatch.setattr(svc, "check_install", lambda: (True, "ok"))
    monkeypatch.setattr(svc, "check_vram", lambda: (True, "ok"))
    monkeypatch.setattr(svc, "_rendre_la_vram_du_worker_ocr", lambda: "ok")

    def resolver(token, supplied_sha256=None):
        return (mask_png, 1024, 1024, "sha")

    monkeypatch.setattr(svc, "_resolve_work_token", resolver)

    def fake_run_edit(*a, **k):
        capture["prompt"] = a[0] if a else k.get("p")
        buf = io.BytesIO()
        Image.new("RGB", (1024, 1024), (1, 2, 3)).save(buf, format="PNG")
        return buf.getvalue(), {
            "seed": k.get("seed", 7),
            "width": 1024,
            "height": 1024,
            "backend": fx.BACKEND_NAME,
        }

    monkeypatch.setattr(fx, "run_edit", fake_run_edit)


def test_generate_empty_prompt_fallback_inpaint_outpaint(tmp_repo, monkeypatch):
    _, _, TestSession = tmp_repo
    for mode in ("inpaint", "outpaint"):
        mask_png = _mask_png(value=200)
        capture = {}
        _mock(svc, fx, monkeypatch, mask_png, capture)
        result = svc.generate(
            None,
            f"run-empty-{mode}",
            "",
            "1:1",
            mode=mode,
            work_token="0" * 32,
            mask_token="0" * 32,
            mask_kind="fill",
            seed=7,
            db_session_factory=TestSession,
            origin="studio",
        )
        assert result is not None
        assert capture["prompt"] == FALLBACK


def test_generate_empty_prompt_rejected_other_modes(tmp_repo, monkeypatch):
    _, _, TestSession = tmp_repo
    monkeypatch.setattr(
        svc, "check_install", lambda: (_ for _ in ()).throw(AssertionError("called"))
    )
    monkeypatch.setattr(svc, "check_vram", lambda: (True, "ok"))
    monkeypatch.setattr(svc, "_rendre_la_vram_du_worker_ocr", lambda: "ok")
    for mode in ("create", "global", "local"):
        extra = {}
        if mode == "local":
            extra = dict(work_token="0" * 32, mask_token="0" * 32, mask_kind="edit")
        elif mode == "global":
            extra = dict(work_token="0" * 32)
        with pytest.raises(ValueError, match="EMPTY_QUERY"):
            svc.generate(
                None,
                f"run-empty-{mode}",
                "",
                "1:1",
                mode=mode,
                seed=7,
                db_session_factory=TestSession,
                **extra,
            )
