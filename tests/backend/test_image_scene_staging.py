"""Staging PNG interne + validation de la source d'édition (sans GPU).

Le staging est borné (token secret, 32 Mio, 0600, écriture atomique) et ne
suit jamais un lien. `load_source_image` applique les mêmes refus que
`_validate_source_artifact`, sans décoder de pixels.
"""

from __future__ import annotations

import hashlib
import io
import os
import stat
from datetime import datetime

import pytest
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.db.models import Artifact, ArtifactLink, Base, Blob
from backend.services import image_service as svc


def _png(width: int = 8, height: int = 8) -> bytes:
    image = Image.new("RGB", (width, height), (10, 20, 30))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    root = tmp_path / "data"
    (root / "blobs").mkdir(parents=True)
    monkeypatch.setattr("backend.config.paths.get_data_dir", lambda: root)
    return root


@pytest.fixture()
def temp_dir(tmp_path, monkeypatch):
    root = tmp_path / "temp"
    monkeypatch.setattr("backend.config.paths.get_temp_dir", lambda: root)
    return root


@pytest.fixture()
def db(tmp_path, data_dir):
    engine = create_engine(f"sqlite:///{tmp_path / 'scene.db'}")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


def _add_image(
    db,
    artifact_id: str,
    *,
    conv_id: str | None = None,
    removed: bool = False,
    deleted: bool = False,
    kind: str = "image",
    scope_kind: str = "conversation",
    data_dir,
) -> bytes:
    data = _png()
    relative_path = f"blobs/{artifact_id}.png"
    (data_dir / relative_path).write_bytes(data)
    db.add(
        Blob(
            id=f"blob_{artifact_id}",
            sha256=f"sha_{artifact_id}",
            relative_path=relative_path,
            size_bytes=len(data),
            mime_detected="image/png",
            state="available",
        )
    )
    db.add(
        Artifact(
            id=artifact_id,
            blob_id=f"blob_{artifact_id}",
            kind=kind,
            original_filename=f"{artifact_id}.png",
            display_name=artifact_id,
            deleted_at=datetime.now() if deleted else None,
        )
    )
    if conv_id:
        db.add(
            ArtifactLink(
                id=f"lnk_{artifact_id}_{conv_id}",
                artifact_id=artifact_id,
                scope_kind=scope_kind,
                scope_id=conv_id,
                role="attachment",
                removed_at=datetime.now() if removed else None,
            )
        )
    db.commit()
    return data


def test_stage_internal_png_token_sha_permissions_et_discard(data_dir, temp_dir):
    data = _png()
    token, sha256 = svc.stage_internal_png(data)

    assert len(token) == 32 and all(c in "0123456789abcdef" for c in token)
    assert sha256 == hashlib.sha256(data).hexdigest()

    staged = temp_dir / "image-studio" / f"{token}.png"
    assert staged.is_file()
    assert staged.read_bytes() == data
    assert stat.S_IMODE(os.stat(staged).st_mode) == 0o600

    svc.discard_staged_tokens([token])
    assert not staged.exists()


def test_discard_ignore_token_invalide_et_hors_racine(data_dir, temp_dir):
    victim = data_dir / "victim.png"
    victim.write_bytes(_png())

    svc.discard_staged_tokens(["../../victim", "pas-un-token", "a" * 32])

    assert victim.is_file()


def test_racine_staging_suit_get_temp_dir(temp_dir):
    token, _ = svc.stage_internal_png(_png())
    assert (temp_dir / "image-studio" / f"{token}.png").is_file()


def test_racine_staging_symlink_refusee(temp_dir):
    temp_dir.mkdir(parents=True, exist_ok=True)
    target = temp_dir / "ailleurs"
    target.mkdir()
    (temp_dir / "image-studio").symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="Racine de staging invalide"):
        svc.stage_internal_png(_png())


def test_check_references_exige_un_lien_actif(db, data_dir):
    _add_image(db, "art_removed", conv_id="conv1", removed=True, data_dir=data_dir)

    with pytest.raises(ValueError, match="SCOPE_DENIED"):
        svc.check_references(db, ["art_removed"], "conv1")


def test_load_source_image_scope_et_suppression(db, data_dir):
    expected = _add_image(db, "art_ok", conv_id="conv1", data_dir=data_dir)
    _add_image(db, "art_autre", conv_id="conv2", data_dir=data_dir)
    _add_image(db, "art_supprime", deleted=True, data_dir=data_dir)

    data, artifact = svc.load_source_image(db, "art_ok", "conv1")
    assert data == expected
    assert getattr(artifact, "id") == "art_ok"

    with pytest.raises(ValueError, match="SCOPE_DENIED"):
        svc.load_source_image(db, "art_autre", "conv1")
    with pytest.raises(ValueError, match="ID_UNKNOWN"):
        svc.load_source_image(db, "art_supprime", None)


def test_load_source_image_lien_retire_et_scope_workspace_denied(db, data_dir):
    _add_image(db, "art_removed", conv_id="conv1", removed=True, data_dir=data_dir)
    _add_image(
        db,
        "art_workspace",
        conv_id="conv1",
        scope_kind="workspace",
        data_dir=data_dir,
    )

    with pytest.raises(ValueError, match="SCOPE_DENIED"):
        svc.load_source_image(db, "art_removed", "conv1")
    with pytest.raises(ValueError, match="SCOPE_DENIED"):
        svc.load_source_image(db, "art_workspace", "conv1")


def test_check_references_refuse_pdf_supprime_et_hors_scope(db, data_dir):
    _add_image(db, "art_pdf", conv_id="conv1", kind="pdf", data_dir=data_dir)
    _add_image(db, "art_deleted", conv_id="conv1", deleted=True, data_dir=data_dir)
    _add_image(
        db,
        "art_workspace",
        conv_id="conv1",
        scope_kind="workspace",
        data_dir=data_dir,
    )

    with pytest.raises(ValueError, match="BAD_ARGS"):
        svc.check_references(db, ["art_pdf"], "conv1")
    with pytest.raises(ValueError, match="ID_UNKNOWN"):
        svc.check_references(db, ["art_deleted"], "conv1")
    with pytest.raises(ValueError, match="SCOPE_DENIED"):
        svc.check_references(db, ["art_workspace"], "conv1")
