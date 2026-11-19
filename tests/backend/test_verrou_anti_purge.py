"""Verrou anti-purge rétroactive (phase 3, tâche 3.8).

Fondement documentaire `cycle_de_vie_des_informations.md` :

  - §4.2 (l. 156) : « Citations déjà présentes dans d'autres discussions :
    conservées comme historique ; leur source peut cesser d'être disponible
    pour un nouvel usage » ;
  - §4.1 (l. 139) : « Les citations déjà présentes dans les messages d'autres
    discussions ne sont pas purgées rétroactivement » ;
  - §4.3 : « Les discussions qui l'ont cité ne sont pas supprimées et leurs
    réponses ne sont pas réécrites rétroactivement » (suppression d'un
    fichier du dépôt) ;
  - `fonctionnalites/espaces.md` §195 : « La suppression d'un fichier du dépôt
    ne doit pas supprimer automatiquement les discussions qui le citaient ».

Ce test verrouille le nettoyage CIBLÉ de la tâche 3.1 : s'il casse, c'est le
nettoyage qui est trop large — corriger le code, jamais ce test. Les objets
cités (artefacts, images, passages) ne subissent jamais la suppression d'une
conversation ou d'un fichier de dépôt en cascade.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.db.migrations import apply_migrations
from backend.db.models import (
    Artifact,
    ArtifactCard,
    ArtifactChunk,
    ArtifactLink,
    Base,
    Blob,
    Conversation,
    DocRef,
    Message,
    MessageAttachment,
    Observation,
)
from backend.search import scope as S
from backend.services import corbeille as C
from backend.services import espaces as E


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """Base temporaire complète (migrations + FTS), SessionLocal du projet."""
    engine = create_engine(f"sqlite:///{tmp_path / 'verrou.db'}")
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


def _conversation(db, conv_id: str, workspace_id: str | None = None) -> str:
    db.add(Conversation(id=conv_id, title=f"Discussion {conv_id}", mode="chat",
                        workspace_id=workspace_id))
    db.add(Message(id=f"msg_{conv_id}", conv_id=conv_id, role="user",
                   content="cité doc_1 ici"))
    db.commit()
    return conv_id


def _artefact_document(db, art_id: str, *, conv_ids: list[str]) -> str:
    """Un document importé (blob, artefact, liens, doc_N), avec ses dérivés.

    Les dérivés (`artifact_chunks`, observation, fiche) sont ce qu'une purge
    aveugle détruirait : le verrou exige leur survie ligne à ligne.
    """
    db.add(Blob(id=f"sha_{art_id}", sha256=f"sha_{art_id}",
                relative_path=f"blobs/sha_{art_id}.bin", size_bytes=200,
                mime_detected="application/pdf", state="available"))
    db.add(Artifact(id=art_id, blob_id=f"sha_{art_id}", kind="pdf",
                    original_filename=f"{art_id}.pdf", display_name=f"{art_id}.pdf"))
    db.add(ArtifactCard(id=f"card_{art_id}", artifact_id=art_id, revision=1,
                        is_current=1, title=f"Fiche {art_id}"))
    for i, conv_id in enumerate(conv_ids):
        db.add(ArtifactLink(id=f"lnk_{art_id}_{conv_id}", artifact_id=art_id,
                            scope_kind="conversation", scope_id=conv_id,
                            role="attachment"))
        db.add(DocRef(id=f"docref_{art_id}_{conv_id}", conv_id=conv_id,
                      doc_label=f"doc_{i + 1}", doc_index=i + 1, artifact_id=art_id))
        db.add(MessageAttachment(id=f"matt_{art_id}_{conv_id}",
                                 message_id=f"msg_{conv_id}",
                                 artifact_id=art_id, ordinal=0))
    db.add(ArtifactChunk(id=f"chk_{art_id}", artifact_id=art_id, ordinal=0,
                         text="passage cité ailleurs", context=""))
    db.add(Observation(id=f"obs_{art_id}", artifact_id=art_id, tool_name="vision_builtin",
                       tool_version="1"))
    db.commit()
    return art_id


def _image_generee(db, art_id: str, conv_ids: list[str]) -> str:
    """Une image générée dans une discussion (lien bibliothèque + liens chats)."""
    db.add(Blob(id=f"sha_{art_id}", sha256=f"sha_{art_id}",
                relative_path=f"blobs/sha_{art_id}.bin", size_bytes=2048,
                mime_detected="image/png", state="available"))
    db.add(Artifact(id=art_id, blob_id=f"sha_{art_id}", kind="image",
                    original_filename=f"{art_id}.png", display_name=f"{art_id}.png"))
    db.add(ArtifactLink(id=f"lnk_{art_id}_library", artifact_id=art_id,
                        scope_kind="library", scope_id="me", role="generated"))
    for conv_id in conv_ids:
        db.add(ArtifactLink(id=f"lnk_{art_id}_{conv_id}", artifact_id=art_id,
                            scope_kind="conversation", scope_id=conv_id,
                            role="generated"))
        db.add(DocRef(id=f"docref_{art_id}_{conv_id}", conv_id=conv_id,
                      doc_label="doc_3", doc_index=3, artifact_id=art_id))
    db.commit()
    return art_id


def _rattache_depot(db, workspace_id: str, art_id: str,
                    role: str = "user_shared") -> str:
    """Le même fichier explicitement déposé dans l'espace (rattachement du dépôt)."""
    db.add(ArtifactLink(id=f"lnk_{art_id}_ws_{workspace_id}", artifact_id=art_id,
                        scope_kind="workspace", scope_id=workspace_id, role=role))
    db.commit()
    return art_id


def _empreinte_des_derives(db, art: str) -> dict[str, int]:
    """Comptes des dérivés citables d'un artefact, lus AVANT la coupure.

    Le verrou est une CONSERVATION, pas une présence attendue : un document
    nourrit passages/observation/fiche, une image n'en nourrit aucun — ce qui
    compte est que la coupure ne détruise RIEN, quel que soit l'inventaire.
    """
    return {
        "passages": db.query(ArtifactChunk).filter(
            ArtifactChunk.artifact_id == art).count(),
        "observations": db.query(Observation).filter(
            Observation.artifact_id == art).count(),
        "fiches": db.query(ArtifactCard).filter(
            ArtifactCard.artifact_id == art).count(),
    }


def _citations_survivent(db, art: str, autre_conv: str, *, frontiere: str,
                         derives: dict[str, int]) -> None:
    """Les citations d'UNE AUTRE discussion vivent, ligne à ligne.

    `frontiere` nomme la cause dans les messages d'échec : la purge de la
    discussion, ou le retrait du dépôt — jamais un `assert` muet. `derives`
    est l'empreinte lue avant la coupure (`_empreinte_des_derives`) : après,
    chaque compte doit être EXACTEMENT le même — la coupure ne fabrique rien
    et ne détruit rien.
    """
    piece = db.query(Artifact).filter(Artifact.id == art).first()
    assert piece is not None, f"{frontiere} : l'artefact cité existe encore"
    assert piece.deleted_at is None, f"{frontiere} : il n'est pas marqué supprimé"
    blob = db.query(Blob).filter(Blob.id == piece.blob_id).first()
    assert blob is not None, f"{frontiere} : les octets de l'artefact cité existent"
    pour_apres = _empreinte_des_derives(db, art)
    assert pour_apres == derives, \
        f"{frontiere} : les dérivés cités sont conservés {derives} → {pour_apres}"
    citation = db.query(DocRef).filter(
        DocRef.conv_id == autre_conv, DocRef.removed_at.is_(None)).all()
    assert citation, f"{frontiere} : la citation de l'autre discussion est intacte"
    liaison = db.query(ArtifactLink).filter(
        ArtifactLink.scope_kind == "conversation",
        ArtifactLink.scope_id == autre_conv,
        ArtifactLink.removed_at.is_(None)).all()
    assert liaison, f"{frontiere} : le lien de l'autre discussion est intact"
    message = db.query(Message).filter(Message.conv_id == autre_conv).all()
    assert message, f"{frontiere} : les réponses de l'autre discussion ne sont pas réécrites"
    piece_jointe = db.query(MessageAttachment).filter(
        MessageAttachment.message_id == f"msg_{autre_conv}").all()
    assert len(piece_jointe) >= 1, f"{frontiere} : la pièce jointe citée reste attachée"


def test_la_purge_d_une_conversation_ne_purge_pas_les_citations_ailleurs(db):
    """Une purge de conversation n'est pas une purge rétroactive (§4.2, l. 156).

    Le verbe `delete` marque la discussion A, puis la purge l'achève. Le
    document et l'image cités par la discussion B, dans un même espace,
    survivent LIGNE À LIGNE, et B garde ses accès : les objets cités ne sont
    jamais supprimés en cascade vers les citations existantes.
    """
    ws = E.create_workspace(db, "Verrou")
    _conversation(db, "conv_a", ws.id)
    _conversation(db, "conv_b", ws.id)
    _artefact_document(db, "art_doc", conv_ids=["conv_a", "conv_b"])
    _image_generee(db, "art_img", conv_ids=["conv_a", "conv_b"])

    assert "art_doc" in S.resolve_allowed_artifacts("conv_b")
    assert "art_img" in S.resolve_allowed_artifacts("conv_b")

    # L'empreinte des dérivés de chaque artefact, lue AVANT la coupure : le
    # verrou est une conservation, pas une présence attendue (`art_doc` est un
    # document nourri ; l'image générée n'a aucun passage ni observation).
    derives_doc = _empreinte_des_derives(db, "art_doc")
    derives_img = _empreinte_des_derives(db, "art_img")

    C.mettre_en_corbeille(db, "conv_a")
    _citations_survivent(db, "art_doc", "conv_b", frontiere="mise à la corbeille",
                         derives=derives_doc)
    _citations_survivent(db, "art_img", "conv_b", frontiere="mise à la corbeille",
                         derives=derives_img)

    # La purge de conv_a achève la suppression : même verrou.
    echue = db.query(Conversation).filter(Conversation.id == "conv_a").first()
    echue.purge_after = echue.purge_after - timedelta(days=8)
    db.commit()  # autoflush=False : l'échéance doit être portée à la base
    C.balayer_corbeille(db)
    assert db.query(Conversation).filter(Conversation.id == "conv_a").first() is None

    _citations_survivent(db, "art_doc", "conv_b", frontiere="purge de la discussion",
                         derives=derives_doc)
    _citations_survivent(db, "art_img", "conv_b", frontiere="purge de la discussion",
                         derives=derives_img)
    assert "art_doc" in S.resolve_allowed_artifacts("conv_b"), \
        "la discussion B garde toujours accès à ce qu'elle cite"
    assert "art_img" in S.resolve_allowed_artifacts("conv_b"), \
        "l'image générée dans A reste à B par le lien de discussion de B"


def test_le_retrait_du_depot_ne_purge_pas_les_discussions_qui_citaient(db):
    """La suppression d'un fichier du dépôt : §4.3, citations intactes.

    Le fichier existe deux fois dans la composition : dépôt de l'espace
    (rattachement explicite) et pièce jointe de discussions. Retirer le dépôt
    coupe le NOUVEL usage, jamais l'historique cité.
    """
    ws = E.create_workspace(db, "Depot")
    _conversation(db, "conv_a", ws.id)
    _conversation(db, "conv_b", ws.id)
    art = _artefact_document(db, "art_doc", conv_ids=["conv_a", "conv_b"])
    _rattache_depot(db, ws.id, "art_doc")
    assert "art_doc" in S.resolve_allowed_artifacts("conv_b")

    derives_doc = _empreinte_des_derives(db, "art_doc")

    retire = E.unshare_artifact(db, ws.id, art)
    assert retire["already_removed"] is False

    _citations_survivent(db, "art_doc", "conv_b", frontiere="retrait du depot",
                         derives=derives_doc)
    # L'accès par la discussion reste : le dépôt n'était qu'un des deux liens.
    assert "art_doc" in S.resolve_allowed_artifacts("conv_b")
    depots = E.depot(db, ws.id)
    assert depots["files"] == [], \
        "le dépôt ne le présente plus : le retrait coupe les NOUVEAUX usages"


def test_un_artifact_cite_par_deux_discussions_survit_a_l_une(db):
    """Un artefact cité par deux discussions survit au départ de l'une.

    La corbeille de A retire le doc_N et le lien de A ; le doc_N et le lien
    de B restent exactement vivants. Un artefact vivant se lit sur son
    artefact (`deleted_at IS NULL`) et sur SES liens actifs, jamais sur ceux
    d'une discussion voisine :
    """
    _conversation(db, "conv_a")
    _conversation(db, "conv_b")
    _artefact_document(db, "art_doc", conv_ids=["conv_a", "conv_b"])

    C.mettre_en_corbeille(db, "conv_a")
    assert "art_doc" in S.resolve_allowed_artifacts("conv_b"), \
        "B garde son artefact cité avec son lien conversationnel"

    # La corbeille retire le doc_N de la discussion supprimée ; celui de B
    # reste exactement une citation vivante, jamais dépubliée par ricochet.
    restants = db.query(DocRef).filter(DocRef.removed_at.is_(None)).all()
    assert [d.conv_id for d in restants] == ["conv_b"], \
        "la corbeille retire le doc_N de la discussion supprimée, pas celui de B"
