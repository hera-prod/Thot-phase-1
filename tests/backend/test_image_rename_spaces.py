"""Galerie : renommage d'une image et regroupement par espace.

Deux contrats tenus ici :

  - `image.rename` ne déplace que l'étiquette (`display_name` + révision de
    fiche) et laisse intacte l'identité (`original_filename`, blob, liens) —
    même règle que le renommage d'un document du dépôt (`depot.md` §5.2) ;
  - `image.list` expose les espaces d'une image (lien `workspace` direct ou
    lien `conversation` d'un espace actif) et sait filtrer dessus, y compris
    « sans espace ».

La base temporaire reprend le fixture de `test_corbeille_galerie.py` : migrations
et index FTS réels, sans toucher au disque du projet.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.db.migrations import apply_migrations
from backend.db.models import (
    Artifact,
    ArtifactCard,
    ArtifactLink,
    Base,
    Blob,
    Conversation,
    Message,
    MessageAttachment,
    Workspace,
)
from backend.ipc import images as I


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """Base temporaire complète (migrations + FTS), SessionLocal du projet."""
    engine = create_engine(f"sqlite:///{tmp_path / 'galerie.db'}")
    Base.metadata.create_all(bind=engine)
    apply_migrations(engine)
    TestSession = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    monkeypatch.setattr("backend.db.database.SessionLocal", TestSession)
    s = TestSession()
    try:
        from backend.search import chunks as _chunks, fts as _fts

        _fts.ensure_fts(s)
        _chunks.ensure_chunk_tables(s)
        yield s
    finally:
        s.close()


def _image(db, art_id: str, *, display_name: str | None = None) -> Artifact:
    """Blob + artefact image + lien bibliothèque : l'image est en galerie."""
    db.add(
        Blob(
            id=f"sha_{art_id}",
            sha256=f"sha_{art_id}",
            relative_path=f"blobs/sha_{art_id}.bin",
            size_bytes=1024,
            mime_detected="image/png",
            state="available",
        )
    )
    art = Artifact(
        id=art_id,
        blob_id=f"sha_{art_id}",
        kind="image",
        original_filename=f"{art_id}.png",
        display_name=display_name or f"{art_id}.png",
    )
    db.add(art)
    db.add(
        ArtifactLink(
            id=f"lnk_{art_id}_library",
            artifact_id=art_id,
            scope_kind="library",
            scope_id="me",
            role="generated",
        )
    )
    return art


def _workspace(db, ws_id: str, name: str, state: str = "active") -> Workspace:
    ws = Workspace(id=ws_id, name=name, state=state)
    db.add(ws)
    return ws


def _conversation(db, conv_id: str, ws_id: str | None) -> Conversation:
    conv = Conversation(id=conv_id, title=conv_id, mode="chat", workspace_id=ws_id)
    db.add(conv)
    return conv


def _link_conversation(db, art_id: str, conv_id: str) -> None:
    db.add(
        ArtifactLink(
            id=f"lnk_{art_id}_{conv_id}",
            artifact_id=art_id,
            scope_kind="conversation",
            scope_id=conv_id,
            role="generated",
        )
    )


def _link_workspace(db, art_id: str, ws_id: str) -> None:
    db.add(
        ArtifactLink(
            id=f"lnk_{art_id}_{ws_id}",
            artifact_id=art_id,
            scope_kind="workspace",
            scope_id=ws_id,
            role="attachment",
        )
    )


def _items_by_id(db, params: dict | None = None) -> dict[str, dict]:
    page = I.image_list(db, {"includeImports": False, **(params or {})})
    return {item["artifact_id"]: item for item in page["items"]}


def test_image_rename_change_display_name_only(db):
    """Renommer change `display_name`, jamais `original_filename` ; idempotent."""
    art = _image(db, "art_renamed", display_name="ancien.png")
    db.commit()

    result = I.image_rename(db, {"artifactId": art.id, "name": "  nouveau.png  "})

    assert result == {
        "ok": True,
        "artifact_id": "art_renamed",
        "changed": True,
        "display_name": "nouveau.png",
    }
    db.refresh(art)
    assert art.display_name == "nouveau.png"
    assert art.original_filename == "art_renamed.png", "l'identité ne bouge pas"

    # Second renommage vers le même nom : aucune révision, aucun changement.
    encore = I.image_rename(db, {"artifact_id": art.id, "display_name": "nouveau.png"})
    assert encore["changed"] is False
    assert encore["display_name"] == "nouveau.png"


def test_image_rename_refuse_nom_vide(db):
    art = _image(db, "art_vide")
    db.commit()

    with pytest.raises(ValueError, match="NAME_REQUIRED"):
        I.image_rename(db, {"artifactId": art.id, "name": "   "})


def test_image_rename_refuse_nom_trop_long(db):
    art = _image(db, "art_long")
    db.commit()

    with pytest.raises(ValueError, match="NAME_TOO_LONG"):
        I.image_rename(db, {"artifactId": art.id, "name": "x" * 201})

    db.refresh(art)
    assert art.display_name == "art_long.png", "le nom n'est pas tronqué"


def test_image_rename_refuse_artefact_non_image(db):
    art = _image(db, "art_texte")
    art.kind = "text"
    db.commit()

    with pytest.raises(ValueError, match="Seules les images"):
        I.image_rename(db, {"artifactId": art.id, "name": "texte.txt"})


def test_image_rename_laisse_blob_liens_et_pieces_jointes_intacts(db):
    """Le renommage ne touche que `display_name` : identité et rattachements intacts."""
    art = _image(db, "art_identite", display_name="ancien.png")
    db.add(Message(id="msg_1", conv_id=None, role="user", content="regarde"))
    db.add(
        MessageAttachment(
            id="att_1",
            message_id="msg_1",
            artifact_id=art.id,
            ordinal=0,
            caption="légende",
        )
    )
    db.commit()

    blob_before = art.blob_id
    link_before = [
        (link.scope_kind, link.scope_id, link.role)
        for link in db.query(ArtifactLink).filter(ArtifactLink.artifact_id == art.id)
    ]
    attachment_before = [
        (att.message_id, att.artifact_id, att.ordinal, att.caption)
        for att in db.query(MessageAttachment).filter(
            MessageAttachment.artifact_id == art.id
        )
    ]

    I.image_rename(db, {"artifactId": art.id, "name": "nouveau.png"})

    db.expire_all()
    renamed = db.get(Artifact, art.id)
    assert renamed is not None
    assert renamed.display_name == "nouveau.png"
    assert renamed.blob_id == blob_before, "le blob ne bouge pas"
    link_after = [
        (link.scope_kind, link.scope_id, link.role)
        for link in db.query(ArtifactLink).filter(ArtifactLink.artifact_id == art.id)
    ]
    attachment_after = [
        (att.message_id, att.artifact_id, att.ordinal, att.caption)
        for att in db.query(MessageAttachment).filter(
            MessageAttachment.artifact_id == art.id
        )
    ]
    assert link_after == link_before
    assert attachment_after == attachment_before


def test_image_rename_refuse_image_absente_ou_supprimee(db):
    """Même refus pour un artefact inconnu et pour un artefact en corbeille."""
    with pytest.raises(ValueError, match="Image introuvable."):
        I.image_rename(db, {"artifactId": "art_inconnu", "name": "x.png"})

    art = _image(db, "art_supprimee")
    art.deleted_at = datetime(2026, 1, 1)
    db.commit()

    with pytest.raises(ValueError, match="Image introuvable."):
        I.image_rename(db, {"artifactId": art.id, "name": "x.png"})


def test_image_rename_revise_la_fiche(db):
    """Une nouvelle fiche courante porte le nouveau titre, le reste est copié."""
    art = _image(db, "art_fiche")
    db.add(
        ArtifactCard(
            id="card_1",
            artifact_id=art.id,
            revision=1,
            is_current=1,
            title="Ancien titre",
            summary='{"prompt": "un chat"}',
            status="indexed",
            analysis_status="indexed",
        )
    )
    db.commit()

    I.image_rename(db, {"artifactId": art.id, "name": "Nouveau titre"})

    from backend.search.cards import get_current_card

    courante = get_current_card(db, art.id)
    assert courante is not None
    assert courante.title == "Nouveau titre"
    assert courante.revision == 2
    assert courante.is_current == 1
    assert courante.summary == '{"prompt": "un chat"}', (
        "un renommage n'efface pas le contenu de la fiche"
    )


def test_image_list_expose_les_espaces(db):
    """Deux chemins d'appartenance : lien direct et lien de conversation."""
    _workspace(db, "ws_1", "Atelier")
    _conversation(db, "conv_1", "ws_1")
    _image(db, "art_direct")
    _image(db, "art_conv")
    _image(db, "art_sans")
    _link_workspace(db, "art_direct", "ws_1")
    _link_conversation(db, "art_conv", "conv_1")
    db.commit()

    items = _items_by_id(db)

    assert items["art_direct"]["spaces"] == [{"id": "ws_1", "name": "Atelier"}]
    assert items["art_conv"]["spaces"] == [{"id": "ws_1", "name": "Atelier"}]
    assert items["art_sans"]["spaces"] == []


def test_image_list_filtre_par_espace(db):
    """`spaceId` cible direct + conversation ; `noSpace` les orphelines."""
    _workspace(db, "ws_1", "Atelier")
    _conversation(db, "conv_1", "ws_1")
    _image(db, "art_direct")
    _image(db, "art_conv")
    _image(db, "art_sans")
    _link_workspace(db, "art_direct", "ws_1")
    _link_conversation(db, "art_conv", "conv_1")
    db.commit()

    assert set(_items_by_id(db, {"spaceId": "ws_1"})) == {"art_direct", "art_conv"}
    assert set(_items_by_id(db, {"noSpace": True})) == {"art_sans"}

    with pytest.raises(ValueError, match="exclusifs"):
        I.image_list(db, {"spaceId": "ws_1", "noSpace": True})


def test_image_list_ignore_espace_en_corbeille(db):
    """Un espace en corbeille ne rattache plus : l'image redevient sans espace."""
    _workspace(db, "ws_corbeille", "Ancien", state="trashed")
    _conversation(db, "conv_1", "ws_corbeille")
    _image(db, "art_conv")
    _link_conversation(db, "art_conv", "conv_1")
    db.commit()

    items = _items_by_id(db)
    assert items["art_conv"]["spaces"] == []
    assert set(_items_by_id(db, {"noSpace": True})) == {"art_conv"}
    assert _items_by_id(db, {"spaceId": "ws_corbeille"}) == {}


def test_image_list_ignore_conversation_en_corbeille(db):
    """Une conversation supprimée ne rattache plus : l'image bascule sans espace."""
    _workspace(db, "ws_actif", "Atelier")
    conv = _conversation(db, "conv_supprimee", "ws_actif")
    conv.deleted_at = datetime(2026, 1, 1)
    _image(db, "art_conv")
    _link_conversation(db, "art_conv", "conv_supprimee")
    db.commit()

    items = _items_by_id(db)
    assert items["art_conv"]["spaces"] == []
    assert set(_items_by_id(db, {"noSpace": True})) == {"art_conv"}
    assert _items_by_id(db, {"spaceId": "ws_actif"}) == {}
