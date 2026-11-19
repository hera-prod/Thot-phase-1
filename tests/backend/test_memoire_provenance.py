"""Provenance dépôt → mémoire (Phase 5).

Ce que ces tests verrouillent, dans l'ordre du plan :

- une pièce jointe et une citation `doc_N` deviennent des sources, une simple
  présence dans le dépôt non ;
- l'invalidation est à DEUX niveaux : une fiche ne devient `stale` que si
  TOUTES ses sources sont perdues ; une `candidate` n'est jamais promue ;
- une perte de portée ne rend rien accessible (fail-closed) : no-op + compteur ;
- `image_delete` et le GC invalident globalement les fiches qui citent
  l'artefact, dans tous les espaces ;
- une suppression de discussion garde une fiche si une source documentaire
  reste accessible ;
- un re-partage réhabilite le statut d'avant, jamais au-delà.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

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
    DocRef,
    MemoryEntry,
    Message,
    MessageAttachment,
    Workspace,
)
from backend.ipc import images as I
from backend.search import trash as T
from backend.services import corbeille as C
from backend.services import espaces as E
from backend.services import memoire as M
from backend.services import memoire_auto as A


@pytest.fixture()
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'provenance.db'}")
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


def _workspace(db, workspace_id: str = "ws_prov") -> str:
    db.add(Workspace(id=workspace_id, name="Espace provenance"))
    db.commit()
    return workspace_id


def _conv(db, conv_id: str, workspace_id: str | None = None) -> str:
    db.add(Conversation(id=conv_id, title=conv_id, workspace_id=workspace_id))
    db.commit()
    return conv_id


def _message(
    db,
    conv_id: str,
    message_id: str,
    content: str,
    *,
    created_at: datetime | None = None,
) -> str:
    db.add(
        Message(
            id=message_id,
            conv_id=conv_id,
            role="user",
            content=content,
            created_at=created_at or datetime(2026, 10, 25, 12, 0, 0),
        )
    )
    db.commit()
    return message_id


def _artifact(db, art_id: str, *, kind: str = "text") -> str:
    if db.query(Artifact).filter(Artifact.id == art_id).first() is None:
        db.add(
            Blob(
                id=f"sha_{art_id}",
                sha256=f"sha_{art_id}",
                relative_path=f"blobs/sha_{art_id}.bin",
                size_bytes=8,
                mime_detected="text/plain",
                state="available",
            )
        )
        db.add(
            Artifact(
                id=art_id,
                blob_id=f"sha_{art_id}",
                kind=kind,
                original_filename=f"{art_id}.txt",
                display_name=f"{art_id}.txt",
            )
        )
        db.commit()
    return art_id


def _link(db, art_id: str, kind: str, scope_id: str, *, removed_at=None) -> None:
    db.add(
        ArtifactLink(
            id=f"lnk_{art_id}_{kind}_{scope_id}",
            artifact_id=art_id,
            scope_kind=kind,
            scope_id=scope_id,
            role="attachment" if kind == "conversation" else "shared",
            removed_at=removed_at,
        )
    )
    db.commit()


def _docref(db, conv_id: str, label: str, art_id: str, index: int) -> None:
    db.add(
        DocRef(
            id=f"doc_{conv_id}_{index}",
            conv_id=conv_id,
            doc_label=label,
            doc_index=index,
            artifact_id=art_id,
        )
    )
    db.commit()


def _courante(db, workspace_id: str) -> MemoryEntry:
    return (
        db.query(MemoryEntry)
        .filter(MemoryEntry.workspace_id == workspace_id, MemoryEntry.is_current == 1)
        .order_by(MemoryEntry.created_at.desc(), MemoryEntry.id)
        .first()
    )


def _fiche_acceptee(db, workspace_id: str, contenu: str, sources: list[str]) -> str:
    out = M.proposer_automatique(
        db,
        workspace_id,
        contenu,
        source_artifact_ids=sources,
        origin_context="discussion:conv_prov:message:msg_1",
        promotion_reasons=[{"kind": "test"}],
        entry_key=f"k:{workspace_id}:{contenu}",
        idempotency_key=f"i:{workspace_id}:{contenu}",
    )
    assert out["ok"] is True, out
    return str(out["entry_id"])


# ---------------------------------------------------------------------------
# 5.1 — alimenter source_artifact_ids
# ---------------------------------------------------------------------------


def test_une_piece_jointe_devient_une_source(db):
    ws = _workspace(db)
    conv = _conv(db, "conv_prov", ws)
    _message(db, conv, "msg_1", "On utilise SQLite comme base de données.")
    art = _artifact(db, "art_piece")
    db.add(
        MessageAttachment(id="matt_1", message_id="msg_1", artifact_id=art, ordinal=0)
    )
    db.commit()

    faits = A.extraire_faits(A.collecter_messages(db, ws), workspace_id=ws, db=db)

    assert len(faits) == 1
    assert faits[0].source_artifact_ids == [art]


def test_une_citation_doc_n_est_une_source_mais_pas_tout_le_depot(db):
    ws = _workspace(db)
    conv = _conv(db, "conv_prov", ws)
    cite = _artifact(db, "art_cite")
    non_cite = _artifact(db, "art_non_cite")
    _link(db, cite, "conversation", conv)
    _link(db, non_cite, "conversation", conv)
    _docref(db, conv, "doc_1", cite, 1)
    _docref(db, conv, "doc_2", non_cite, 2)
    _message(
        db,
        conv,
        "msg_1",
        "On utilise SQLite comme base de données ; le détail est dans doc_1.",
    )

    faits = A.extraire_faits(A.collecter_messages(db, ws), workspace_id=ws, db=db)

    assert len(faits) == 1
    assert faits[0].source_artifact_ids == [cite]
    assert non_cite not in faits[0].source_artifact_ids


def test_ajouter_preuve_fusionne_la_nouvelle_piece_sans_version(db):
    ws = _workspace(db)
    conv = _conv(db, "conv_prov", ws)
    art1 = _artifact(db, "art_1")
    art2 = _artifact(db, "art_2")
    _link(db, art1, "workspace", ws)
    _link(db, art2, "workspace", ws)
    eid = _fiche_acceptee(db, ws, "On utilise SQLite comme base.", [art1])
    version = int(_courante(db, ws).version)

    out = M.ajouter_preuve(
        db,
        eid,
        origin_context="discussion:conv_prov:message:msg_2",
        evidence_ids=["msg_2"],
        source_artifact_ids=[art2, art2],
        workspace_id=ws,
    )

    assert out["ok"] is True
    assert out["source_artifact_ids"] == [art1, art2]
    courante = _courante(db, ws)
    assert courante.version == version, "la provenance ne crée pas de version"
    assert courante.source_artifact_ids == [art1, art2]


def test_une_nouvelle_piece_jointe_fusionne_sur_la_fiche_existante(db):
    ws = _workspace(db)
    conv = _conv(db, "conv_prov", ws)
    art1 = _artifact(db, "art_1")
    art2 = _artifact(db, "art_2")
    _link(db, art1, "conversation", conv)
    _link(db, art2, "conversation", conv)
    _message(db, conv, "msg_1", "On utilise SQLite comme base de données.")
    db.add(
        MessageAttachment(id="matt_1", message_id="msg_1", artifact_id=art1, ordinal=0)
    )
    db.commit()
    A.forcer_memoire_espace(db, ws)
    assert _courante(db, ws).source_artifact_ids == [art1]

    _message(
        db,
        conv,
        "msg_2",
        "On utilise SQLite comme base de données.",
        created_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1),
    )
    db.add(
        MessageAttachment(id="matt_2", message_id="msg_2", artifact_id=art2, ordinal=0)
    )
    db.commit()
    A.forcer_memoire_espace(db, ws)

    fiche = _courante(db, ws)
    assert fiche.status == "accepted"
    assert fiche.source_artifact_ids == [art1, art2]
    assert fiche.version == 1, "la provenance ne crée pas de version"


def test_max_sources_et_deduplication(db):
    ws = _workspace(db)
    conv = _conv(db, "conv_prov", ws)
    _message(db, conv, "msg_1", "On utilise SQLite comme base de données.")
    artefacts = [f"art_{i:02d}" for i in range(40)]
    for index, art in enumerate(artefacts):
        _artifact(db, art)
        db.add(
            MessageAttachment(
                id=f"matt_{index}",
                message_id="msg_1",
                artifact_id=art,
                ordinal=index,
            )
        )
    # Une pièce jointe dupliquée ne doit pas consommer une place.
    db.add(
        MessageAttachment(
            id="matt_dup",
            message_id="msg_1",
            artifact_id=artefacts[0],
            ordinal=99,
        )
    )
    db.commit()

    faits = A.extraire_faits(A.collecter_messages(db, ws), workspace_id=ws, db=db)

    assert len(faits) == 1
    sources = faits[0].source_artifact_ids
    assert len(sources) == M.MAX_SOURCES
    assert len(set(sources)) == len(sources)


# ---------------------------------------------------------------------------
# 5.2/5.3 — invalidation à deux niveaux
# ---------------------------------------------------------------------------


def test_deux_sources_une_perdue_garde_le_statut_puis_stale_si_toutes_perdues(db):
    ws = _workspace(db)
    _conv(db, "conv_prov", ws)
    art1 = _artifact(db, "art_1")
    art2 = _artifact(db, "art_2")
    _link(db, art1, "workspace", ws)
    _link(db, art2, "workspace", ws)
    _fiche_acceptee(db, ws, "On utilise SQLite comme base.", [art1, art2])

    E.unshare_artifact(db, ws, art1)

    fiche = _courante(db, ws)
    assert fiche.status == "accepted", "une source perdue n'est pas toutes les sources"
    assert fiche.source_artifact_ids == [art1, art2], "la provenance n'est pas nettoyée"

    E.unshare_artifact(db, ws, art2)

    fiche = _courante(db, ws)
    assert fiche.status == "stale"
    assert fiche.stale_reason == M.STALE_REASON_SOURCE_INACCESSIBLE
    assert fiche.previous_status == "accepted"


def test_candidate_partielle_n_est_jamais_promise(db):
    ws = _workspace(db)
    _conv(db, "conv_prov", ws)
    art1 = _artifact(db, "art_1")
    art2 = _artifact(db, "art_2")
    _link(db, art1, "workspace", ws)
    _link(db, art2, "workspace", ws)
    eid = M.propose(
        db,
        ws,
        "Un fait encore candidat.",
        source_artifact_ids=[art1, art2],
        origin_context="discussion:conv_prov:message:msg_1",
        promotion_reasons=[{"kind": "test"}],
        idempotency_key="idem:candidate",
        entry_key="k:candidate",
    )["entry_id"]

    E.unshare_artifact(db, ws, art1)

    fiche = M._current(db, eid)
    assert fiche.status == "candidate"
    # Toutes perdues : la candidate devient stale, mais ne devient jamais
    # accepted — une invalidation n'est pas une promotion.
    E.unshare_artifact(db, ws, art2)
    fiche = M._current(db, eid)
    assert fiche.status == "stale"
    assert fiche.previous_status == "candidate"


def test_artefact_partage_entre_deux_espaces_ne_perime_que_le_premier(db):
    ws_a = _workspace(db, "ws_a")
    ws_b = _workspace(db, "ws_b")
    _conv(db, "conv_a", ws_a)
    _conv(db, "conv_b", ws_b)
    art = _artifact(db, "art_partage")
    _link(db, art, "workspace", ws_a)
    _link(db, art, "workspace", ws_b)
    _fiche_acceptee(db, ws_a, "Le fait de A.", [art])
    _fiche_acceptee(db, ws_b, "Le fait de B.", [art])

    E.unshare_artifact(db, ws_a, art)

    assert _courante(db, ws_a).status == "stale"
    assert _courante(db, ws_b).status == "accepted"


# ---------------------------------------------------------------------------
# 5.4 — réhabilitation
# ---------------------------------------------------------------------------


def test_repartage_rehabilite_previous_status_sans_promouvoir_une_candidate(db):
    ws = _workspace(db)
    conv_orpheline = _conv(db, "conv_orpheline", None)
    art = _artifact(db, "art_rehab")
    _link(db, art, "conversation", conv_orpheline)
    E.share_artifact(db, ws, art, from_conv_id=conv_orpheline)
    eid = _fiche_acceptee(db, ws, "Un fait réhabilitable.", [art])

    E.unshare_artifact(db, ws, art)
    fiche = M._current(db, eid)
    assert fiche.status == "stale"
    assert fiche.previous_status == "accepted"
    assert fiche.stale_reason == M.STALE_REASON_SOURCE_INACCESSIBLE

    E.share_artifact(db, ws, art, from_conv_id=conv_orpheline)

    fiche = M._current(db, eid)
    assert fiche.status == "accepted"
    assert fiche.stale_reason is None
    assert fiche.previous_status is None

    # Une candidate réhabilitée reste candidate : jamais de promotion gratuite.
    conv2 = _conv(db, "conv_candidate", None)
    art2 = _artifact(db, "art_rehab_candidate")
    _link(db, art2, "conversation", conv2)
    E.share_artifact(db, ws, art2, from_conv_id=conv2)
    candidate = M.propose(
        db,
        ws,
        "Un fait candidat réhabilitable.",
        source_artifact_ids=[art2],
        origin_context="discussion:conv_candidate:message:msg_1",
        promotion_reasons=[{"kind": "test"}],
        idempotency_key="idem:rehab:candidate",
        entry_key="k:rehab:candidate",
    )["entry_id"]
    E.unshare_artifact(db, ws, art2)
    assert M._current(db, candidate).status == "stale"
    E.share_artifact(db, ws, art2, from_conv_id=conv2)
    assert M._current(db, candidate).status == "candidate"


def test_une_stale_pour_suppression_ne_se_reactive_pas_par_un_partage(db):
    ws = _workspace(db)
    conv = _conv(db, "conv_prov", ws)
    _message(db, conv, "msg_1", "On utilise SQLite comme base de données.")
    A.forcer_memoire_espace(db, ws)
    fiche = _courante(db, ws)
    assert fiche.status == "accepted"
    M.mark_stale(
        db,
        str(fiche.id),
        reason="suppression volontaire",
        stale_reason=M.STALE_REASON_DISCUSSION_SUPPRIMEE,
        workspace_id=ws,
    )

    # La fiche cite la source mais n'a pas été périmée pour cette raison : la
    # synchronisation ne doit pas la réhabiliter.
    _conv(db, "conv_autre", ws)
    art = _artifact(db, "art_autre")
    _link(db, art, "workspace", ws)
    M.ajouter_preuve(
        db,
        str(M._current(db, fiche.id).id),
        origin_context=None,
        source_artifact_ids=[art],
        workspace_id=ws,
    )
    M.syncer_validite_sources(db, ws)
    assert M._current(db, fiche.id).status == "stale"


# ---------------------------------------------------------------------------
# 5.5 — suppression de discussion et preuves
# ---------------------------------------------------------------------------


def test_suppression_de_discussion_avec_source_documentaire_restante(db):
    ws = _workspace(db)
    conv = _conv(db, "conv_prov", ws)
    _message(db, conv, "msg_1", "On utilise SQLite comme base de données.")
    art = _artifact(db, "art_doc")
    _link(db, art, "conversation", conv)
    _link(db, art, "workspace", ws)
    db.add(
        MessageAttachment(id="matt_1", message_id="msg_1", artifact_id=art, ordinal=0)
    )
    db.commit()
    A.forcer_memoire_espace(db, ws)
    assert _courante(db, ws).source_artifact_ids == [art]

    C.mettre_en_corbeille(db, conv)

    fiche = _courante(db, ws)
    assert fiche.status == "accepted", "une source documentaire reste accessible"
    assert fiche.origin_context is None
    assert fiche.source_artifact_ids == [art]


def test_suppression_de_discussion_sans_source_restante_est_stale(db):
    ws = _workspace(db)
    conv = _conv(db, "conv_prov", ws)
    _message(db, conv, "msg_1", "On utilise SQLite comme base de données.")
    art = _artifact(db, "art_conv")
    _link(db, art, "conversation", conv)
    db.add(
        MessageAttachment(id="matt_1", message_id="msg_1", artifact_id=art, ordinal=0)
    )
    db.commit()
    A.forcer_memoire_espace(db, ws)

    C.mettre_en_corbeille(db, conv)

    fiche = _courante(db, ws)
    assert fiche.status == "stale"
    assert fiche.stale_reason == M.STALE_REASON_DISCUSSION_SUPPRIMEE


def test_invalider_conversation_retire_les_preuves_doc_et_messages(db):
    ws = _workspace(db)
    conv = _conv(db, "conv_prov", ws)
    _message(db, conv, "msg_1", "On utilise SQLite comme base de données.")
    art = _artifact(db, "art_doc")
    _link(db, art, "conversation", conv)
    _docref(db, conv, "doc_1", art, 1)
    fiche = _fiche_acceptee(db, ws, "Un fait sourcé.", [art])
    M.ajouter_preuve(
        db,
        fiche,
        origin_context="discussion:conv_prov:message:msg_1",
        evidence_ids=["msg_1", "doc_1"],
        workspace_id=ws,
    )

    A.invalider_conversation(db, conv)

    courante = M._current(db, fiche)
    assert "msg_1" not in list(courante.evidence_ids or [])
    assert "doc_1" not in list(courante.evidence_ids or [])
    # La source partagée n'est pas retirée aveuglément.
    assert courante.source_artifact_ids == [art]


# ---------------------------------------------------------------------------
# 5.2/5.6 — fail-closed et observabilité
# ---------------------------------------------------------------------------


def test_erreur_de_scope_est_un_noop_avec_compteur_et_file(db, monkeypatch):
    ws = _workspace(db)
    _conv(db, "conv_prov", ws)
    art = _artifact(db, "art_1")
    _link(db, art, "workspace", ws)
    _fiche_acceptee(db, ws, "Un fait stable.", [art])
    M.reinitialiser_compteur_sync()

    def _panne(*_args, **_kwargs):
        raise RuntimeError("scope illisible")

    monkeypatch.setattr("backend.search.scope.resolve_space_information_scope", _panne)

    out = M.syncer_validite_sources(db, ws)

    assert out["ok"] is True and out.get("skipped") is True
    assert _courante(db, ws).status == "accepted", "aucune invalidation sur une panne"
    assert M.lire_compteur_sync() == 1
    assert ws in M.file_reprise_sync()


# ---------------------------------------------------------------------------
# 5.2 — invalidation globale (image_delete, GC)
# ---------------------------------------------------------------------------


def test_image_delete_perime_la_fiche_dans_tous_les_espaces(db):
    ws_a = _workspace(db, "ws_a")
    ws_b = _workspace(db, "ws_b")
    _conv(db, "conv_a", ws_a)
    _conv(db, "conv_b", ws_b)
    image = _artifact(db, "art_image", kind="image")
    _link(db, image, "workspace", ws_a)
    _link(db, image, "workspace", ws_b)
    _fiche_acceptee(db, ws_a, "Fait lié à l'image A.", [image])
    _fiche_acceptee(db, ws_b, "Fait lié à l'image B.", [image])

    out = I.image_delete(db, {"artifactId": image})

    assert out["ok"] is True
    assert _courante(db, ws_a).status == "stale"
    assert _courante(db, ws_b).status == "stale"


def test_gc_de_la_corbeille_perime_la_fiche_qui_citait_l_artefact(db):
    ws = _workspace(db)
    _conv(db, "conv_prov", ws)
    art = _artifact(db, "art_gc")
    _link(db, art, "workspace", ws)
    _fiche_acceptee(db, ws, "Fait lié à un artefact purgé.", [art])
    # La source devient éligible au GC APRÈS la création de la fiche.
    vieux = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=8)
    lien = (
        db.query(ArtifactLink)
        .filter(
            ArtifactLink.artifact_id == art,
            ArtifactLink.scope_kind == "workspace",
            ArtifactLink.scope_id == ws,
        )
        .one()
    )
    lien.removed_at = vieux
    db.commit()

    rapport = T.purge(db)

    assert rapport["artifacts"] == 1
    fiche = _courante(db, ws)
    assert fiche.status == "stale"
    assert fiche.stale_reason == M.STALE_REASON_SOURCE_INACCESSIBLE


# ---------------------------------------------------------------------------
# 5.7 — champs exposés
# ---------------------------------------------------------------------------


def test_stale_reason_et_previous_status_sont_exposes(db):
    ws = _workspace(db)
    _conv(db, "conv_prov", ws)
    art = _artifact(db, "art_1")
    _link(db, art, "workspace", ws)
    _fiche_acceptee(db, ws, "Un fait exposé.", [art])
    E.unshare_artifact(db, ws, art)

    element = next(e for e in M.lister_elements_courants(db, ws) if e["entry_id"])
    assert element["status"] == "stale"
    assert element["stale_reason"] == M.STALE_REASON_SOURCE_INACCESSIBLE
    assert element["previous_status"] == "accepted"

    fait = next(
        f for f in M.faits_courants(db, ws) if f["entry_id"] == element["entry_id"]
    )
    assert fait["stale_reason"] == M.STALE_REASON_SOURCE_INACCESSIBLE
    assert fait["previous_status"] == "accepted"
