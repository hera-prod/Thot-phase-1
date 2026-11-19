"""Tests ciblés pour le contrat mask_kind / composite_protected (backend seul)."""

from __future__ import annotations

import io
from PIL import Image

import pytest

from backend.services import flux_spike as fx
from backend.services import image_service as svc


def _png_bytes(size=(4, 4), mode="L", fill=0, hot=None):
    img = Image.new(mode, size, fill)
    if hot is not None:
        x, y, v = hot
        img.putpixel((x, y), v)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _mask_png(size=(1024, 1024), value=200):
    return _png_bytes(size=size, mode="L", fill=0, hot=(10, 10, value))


@pytest.fixture()
def tmp_repo(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from backend.db.models import Base

    db_file = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    monkeypatch.setattr("backend.db.database.SessionLocal", TestSession)
    monkeypatch.setenv("THOT_IMAGE_OPTIN_PATH", str(tmp_path / "image_backend.optin"))
    monkeypatch.delenv("THOT_IMAGE_BACKEND", raising=False)
    data_dir = tmp_path / "data"
    (data_dir / "blobs" / "ab").mkdir(parents=True)
    monkeypatch.setattr("backend.config.paths.get_data_dir", lambda: data_dir)
    return tmp_path, data_dir, TestSession


def _mock_accepted(svc, fx, monkeypatch, mask_png):
    monkeypatch.setattr(svc, "check_install", lambda: (True, "ok"))
    monkeypatch.setattr(svc, "check_vram", lambda: (True, "ok"))
    monkeypatch.setattr(svc, "_rendre_la_vram_du_worker_ocr", lambda: "ok")

    def resolver(token, supplied_sha256=None):
        return (mask_png, 1024, 1024, "sha")

    monkeypatch.setattr(svc, "_resolve_work_token", resolver)
    called = {"run_edit": 0, "mode": None}

    def fake_run_edit(*a, **k):
        called["run_edit"] += 1
        called["mode"] = k.get("mode")
        from backend.services import flux_spike as _fx
        buf = io.BytesIO()
        Image.new("RGB", (1024, 1024), (1, 2, 3)).save(buf, format="PNG")
        return buf.getvalue(), {
            "seed": k.get("seed", 7),
            "width": 1024,
            "height": 1024,
            "backend": _fx.BACKEND_NAME,
        }

    monkeypatch.setattr(fx, "run_edit", fake_run_edit)
    return called


def test_local_edit_accepted(tmp_repo, monkeypatch):
    _, _, TestSession = tmp_repo
    mask_png = _mask_png(value=200)
    called = _mock_accepted(svc, fx, monkeypatch, mask_png)
    result = svc.generate(
        None,
        "run-local-edit",
        "prompt",
        "1:1",
        mode="local",
        work_token="0" * 32,
        mask_token="0" * 32,
        mask_kind="edit",
        seed=7,
        db_session_factory=TestSession,
        origin="studio",
    )
    assert called["run_edit"] == 1
    assert called["mode"] == "local"
    assert result["stats"]["mask_kind"] == "edit"
    from backend.db.models import ArtifactCard
    import json

    db = TestSession()
    try:
        card = db.query(ArtifactCard).filter(ArtifactCard.artifact_id == result["artifact_id"]).first()
        prov = json.loads(card.summary)
    finally:
        db.close()
    assert prov.get("mask_kind") == "edit"


def test_local_fill_rejected(tmp_repo, monkeypatch):
    _, _, TestSession = tmp_repo
    monkeypatch.setattr(svc, "check_install", lambda: (_ for _ in ()).throw(AssertionError("called")))
    monkeypatch.setattr(svc, "check_vram", lambda: (True, "ok"))
    monkeypatch.setattr(svc, "_rendre_la_vram_du_worker_ocr", lambda: "ok")
    with pytest.raises(ValueError, match="MASK_KIND_MISMATCH"):
        svc.generate(
            None,
            "run-local-fill",
            "prompt",
            "1:1",
            mode="local",
            work_token="0" * 32,
            mask_token="0" * 32,
            mask_kind="fill",
            seed=7,
            db_session_factory=TestSession,
        )


def test_inpaint_outpaint_fill_accepted(tmp_repo, monkeypatch):
    _, _, TestSession = tmp_repo
    for mode in ("inpaint", "outpaint"):
        mask_png = _mask_png(value=200)
        called = _mock_accepted(svc, fx, monkeypatch, mask_png)
        result = svc.generate(
            None,
            f"run-{mode}",
            "prompt",
            "1:1",
            mode=mode,
            work_token="0" * 32,
            mask_token="0" * 32,
            mask_kind="fill",
            seed=7,
            db_session_factory=TestSession,
            origin="studio",
        )
        assert called["mode"] == mode
        assert result["stats"]["mask_kind"] == "fill"


def test_inpaint_edit_rejected(tmp_repo, monkeypatch):
    _, _, TestSession = tmp_repo
    monkeypatch.setattr(svc, "check_install", lambda: (_ for _ in ()).throw(AssertionError("called")))
    monkeypatch.setattr(svc, "check_vram", lambda: (True, "ok"))
    monkeypatch.setattr(svc, "_rendre_la_vram_du_worker_ocr", lambda: "ok")
    with pytest.raises(ValueError, match="MASK_KIND_MISMATCH"):
        svc.generate(
            None,
            "run-inpaint-edit",
            "prompt",
            "1:1",
            mode="inpaint",
            work_token="0" * 32,
            mask_token="0" * 32,
            mask_kind="edit",
            seed=7,
            db_session_factory=TestSession,
        )


def test_missing_mask_kind_on_edit_modes_rejected(tmp_repo, monkeypatch):
    _, _, TestSession = tmp_repo
    monkeypatch.setattr(svc, "check_install", lambda: (_ for _ in ()).throw(AssertionError("called")))
    monkeypatch.setattr(svc, "check_vram", lambda: (True, "ok"))
    monkeypatch.setattr(svc, "_rendre_la_vram_du_worker_ocr", lambda: "ok")
    for mode in ("local", "inpaint", "outpaint"):
        with pytest.raises(ValueError, match="BAD_ARGS|MASK_KIND_MISMATCH"):
            svc.generate(
                None,
                f"run-missing-{mode}",
                "prompt",
                "1:1",
                mode=mode,
                work_token="0" * 32,
                mask_token="0" * 32,
                db_session_factory=TestSession,
            )


def test_mask_kind_on_global_rejected(tmp_repo, monkeypatch):
    _, _, TestSession = tmp_repo
    monkeypatch.setattr(svc, "check_install", lambda: (_ for _ in ()).throw(AssertionError("called")))
    monkeypatch.setattr(svc, "check_vram", lambda: (True, "ok"))
    monkeypatch.setattr(svc, "_rendre_la_vram_du_worker_ocr", lambda: "ok")
    with pytest.raises(ValueError, match="BAD_ARGS|MASK_KIND_MISMATCH"):
        svc.generate(
            None,
            "run-global-mk",
            "prompt",
            "1:1",
            mode="global",
            work_token="0" * 32,
            mask_kind="fill",
            db_session_factory=TestSession,
        )


def test_empty_mask_rejected(tmp_repo, monkeypatch):
    _, _, TestSession = tmp_repo
    empty_mask = _png_bytes(size=(1024, 1024), mode="L", fill=0)
    monkeypatch.setattr(svc, "check_install", lambda: (_ for _ in ()).throw(AssertionError("called")))
    monkeypatch.setattr(svc, "check_vram", lambda: (True, "ok"))
    monkeypatch.setattr(svc, "_rendre_la_vram_du_worker_ocr", lambda: "ok")

    def resolver(token, supplied_sha256=None):
        return (empty_mask, 1024, 1024, "sha")

    monkeypatch.setattr(svc, "_resolve_work_token", resolver)
    with pytest.raises(ValueError, match="MASK_EMPTY"):
        svc.generate(
            None,
            "run-empty-mask",
            "prompt",
            "1:1",
            mode="local",
            work_token="0" * 32,
            mask_token="0" * 32,
            mask_kind="edit",
            seed=7,
            db_session_factory=TestSession,
        )


def test_composite_protected_exact_pixels_and_alpha():
    work = Image.new("RGBA", (4, 2), (10, 20, 30, 200))
    generated = Image.new("RGBA", (4, 2), (200, 100, 50, 255))
    mask = Image.new("L", (4, 2), 0)
    mask.putpixel((0, 0), 127)
    mask.putpixel((1, 0), 128)
    mask.putpixel((2, 0), 0)
    mask.putpixel((3, 0), 255)

    def to_bytes(img):
        b = io.BytesIO()
        img.save(b, format="PNG")
        return b.getvalue()

    out_bytes = fx.composite_protected(to_bytes(generated), to_bytes(work), to_bytes(mask))
    out = Image.open(io.BytesIO(out_bytes)).convert("RGBA")

    assert out.getpixel((0, 0)) == (10, 20, 30, 200)
    assert out.getpixel((2, 0)) == (10, 20, 30, 200)
    assert out.getpixel((1, 0)) == (200, 100, 50, 255)
    assert out.getpixel((3, 0)) == (200, 100, 50, 255)


def test_composite_protected_threshold_128_boundary():
    work = Image.new("RGBA", (2, 1), (1, 2, 3, 4))
    generated = Image.new("RGBA", (2, 1), (5, 6, 7, 8))
    mask = Image.new("L", (2, 1), 0)
    mask.putpixel((0, 0), 127)
    mask.putpixel((1, 0), 128)

    def to_bytes(img):
        b = io.BytesIO()
        img.save(b, format="PNG")
        return b.getvalue()

    out_bytes = fx.composite_protected(to_bytes(generated), to_bytes(work), to_bytes(mask))
    out = Image.open(io.BytesIO(out_bytes)).convert("RGBA")
    assert out.getpixel((0, 0)) == (1, 2, 3, 4)
    assert out.getpixel((1, 0)) == (5, 6, 7, 255)
