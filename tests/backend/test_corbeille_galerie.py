"""Images préservées à la suppression d'une discussion (phase 3, tâche 3.2).

Fondement documentaire (fiches lues, lecture seule) :

  - `cycle_de_vie_des_informations.md` §4.1 : « Les images jointes ou générées
    à préserver sont détachées et deviennent des images autonomes de galerie :
    elles sortent du corpus de mémoire de l'espace associé à la discussion et
    ne réapparaissent pas dans une recherche d'espace seulement parce que la
    galerie les conserve » — et son tableau §4.2, « conservées comme images
    autonomes de galerie, détachées et hors du corpus de mémoire de l'espace » ;
  - `fonctionnalites/galerie.md` §3.2 : « l'image préservée est retirée du
    corpus de mémoire de l'espace associé à cette discussion : elle ne
    réapparaît pas dans une recherche d'espace seulement parce que la galerie
    la conserve », et §3.3 (représentations conservées « dans le périmètre
    galerie, sans maintenir l'accès de l'ancien chat »).

Le lien `library/generated` existe déjà à la génération
(`backend/services/flux_spike.py` pose scope `library` + scope `conversation`) ;
la mise en corbeille retire le lien conversationnel, et le lien bibliothèque
porte seul l'image en galerie. Ce fichier verrouille le résultat observable :
galerie, corpus d'espace, recherche d'espace.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.db.migrations import apply_migrations
from backend.db.models import (
    Artifact,
    ArtifactLink,
    Base,
    Blob,
    Conversation,
)
from backend.services import corbeille as C
from backend.services import espaces as E


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """Base temporaire complète (migrations + FTS), SessionLocal du projet."""
    engine = create_engine(f"sqlite:///{tmp_path / 'galerie.db'}")
    Base.metadata.create_all(bind=engine)
    apply_migrations(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False,
                               autocommit=False, expire_on_commit=False)
    monkeypatch.setattr("backend.db.database.SessionLocal", TestSession)
    s = TestSession()
    try:
        from backend.search import chunks as _chunks, fts as _fts

        _fts.ensure_fts(s)
        _chunks.ensure_chunk_tables(s)
        yield s
    finally:
        s.close()


def _image_generee(db, art_id: str, conv_id: str) -> str:
    """Une image générée dans une discussion : blob + artefact + DEUX liens.

    Exactement l'écriture de `flux_spike._publier` : le lien bibliothèque
    (`library/me`, `generated`) rend l'image galerie, le lien discussion
    (`conversation/{conv_id}`, `generated`) la rend à son chat d'origine.
    """
    db.add(Blob(id=f"sha_{art_id}", sha256=f"sha_{art_id}",
                relative_path=f"blobs/sha_{art_id}.bin", size_bytes=2048,
                mime_detected="image/png", state="available"))
    db.add(Artifact(id=art_id, blob_id=f"sha_{art_id}", kind="image",
                    original_filename=f"{art_id}.png", display_name=f"{art_id}.png"))
    db.add(ArtifactLink(id=f"lnk_{art_id}_library", artifact_id=art_id,
                        scope_kind="library", scope_id="me", role="generated"))
    db.add(ArtifactLink(id=f"lnk_{art_id}_{conv_id}", artifact_id=art_id,
                        scope_kind="conversation", scope_id=conv_id, role="generated"))
    db.commit()
    return art_id


def _lecture_galerie(db):
    """La page galerie vue par le pont (`ipc/images.image_list`, imports exclus)."""
    from backend.ipc import images as I

    return {i["artifact_id"] for i in I.image_list(db, {"includeImports": False})["items"]}


def test_deux_images_generees_survolent_la_suppression(db):
    """La suppression d'une discussion, deux images : elles restent en galerie.

    L'ancien DELETE en cascade les emportait avec la conversation ; la corbeille
    retire LE LIEN conversationnel, jamais le blob ni l'artefact (cycle de vie
    §4.2, « images jointes ou générées préservées »).
    """
    ws = E.create_workspace(db, "Images")
    db.add(Conversation(id="conv_1", title="Atelier", mode="chat",
                        workspace_id=ws.id))
    img1 = _image_generee(db, "art_img1", "conv_1")
    img2 = _image_generee(db, "art_img2", "conv_1")
    db.commit()

    assert _lecture_galerie(db) == {img1, img2}, "les deux images sont en galerie AVANT"
    C.mettre_en_corbeille(db, "conv_1")

    assert _lecture_galerie(db) == {img1, img2}, \
        "les deux images RESTENT en galerie après la mise en corbeille"
    artefacts = db.query(Artifact).filter(Artifact.kind == "image").all()
    assert len(artefacts) == 2
    assert all(a.deleted_at is None for a in artefacts), \
        "aucune image n'est marquée supprimée"
    # Les liens conversationnels sont dépubliés, les liens bibliothèque vivants.
    scopes = {
        (l.artifact_id, l.scope_kind, l.role)
        for l in db.query(ArtifactLink).filter(ArtifactLink.removed_at.is_(None)).all()
    }
    assert (img1, "library", "generated") in scopes
    assert (img2, "library", "generated") in scopes
    convos = [s for (_, s, _) in scopes if s == "conversation"]
    assert convos == [], "plus aucun lien conversationnel actif après la corbeille"


def test_les_images_sortent_du_corpus_et_de_la_recherche_de_espace(db):
    """Hors corpus de mémoire de l'espace, hors recherche d'espace.

    AVANT la corbeille, les images générées DANS une discussion de l'espace
    alimentent le périmètre d'information (`resolve_space_information_scope`
    inclut les artefacts liés aux discussions de l'espace — `galerie.md` §2 ne
    les y met pas, c'est le rattachement de discussion qui le fait). APRÈS, la
    corbeille les en tire : ni corpus, ni recherche.
    """
    from backend.search import scope as S

    ws = E.create_workspace(db, "Atelier")
    db.add(Conversation(id="conv_1", title="Atelier", mode="chat",
                        workspace_id=ws.id))
    img1 = _image_generee(db, "art_img1", "conv_1")
    img2 = _image_generee(db, "art_img2", "conv_1")
    db.commit()

    avant = S.resolve_space_information_scope(ws.id)
    assert {img1, img2} <= avant, "les images alimentaient le corpus AVANT la corbeille"

    C.mettre_en_corbeille(db, "conv_1")

    apres = S.resolve_space_information_scope(ws.id)
    assert img1 not in apres and img2 not in apres, \
        "les images sont hors corpus de mémoire de l'espace"
    assert E.depot(db, ws.id)["files"] == [], "le dépôt n'absorbe pas les images"
    # Et la recherche de portée espace ne les voit pas : le corpus est la seule
    # source de candidats d'`space_info`, ses index conversationnels sont partis.
    from backend.search import space_info as SI

    page = SI.search_space_information(db, ws.id, "img")
    rendus = {r.get("artifact_id") for r in page.get("results", [])}
    assert not ({img1, img2} & rendus), "aucune image dans une recherche d'espace"


def test_une_discussion_ne_regarde_plus_ses_images_detachees(db):
    """Le chat d'origine, lui, ne voit plus rien : lien conversation retiré.

    `galerie.md` §3.3 : une image rattachée n'entre dans la recherche d'une
    discussion que par SON rattachement ; le rattachement est parti avec la
    corbeille. §1 : la galerie reste une collection DISTINCTE des corpus.
    """
    from backend.search import scope as S

    db.add(Conversation(id="conv_1", title="Atelier", mode="chat"))
    img = _image_generee(db, "art_img1", "conv_1")
    assert img in S.resolve_allowed_artifacts("conv_1")
    C.mettre_en_corbeille(db, "conv_1")
    assert S.resolve_allowed_artifacts("conv_1") == set(), \
        "une discussion en corbeille n'accorde rien"
    # Et la galerie garde la clef bibliothèque, pas une fuite d'ancien chat.
    assert img in _lecture_galerie(db)
