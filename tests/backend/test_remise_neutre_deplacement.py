"""Tests de la remise à neutre des signaux d'importance au déplacement — task 5.7.

Règle : transfert + synthèses actualisées + signaux déplacés remis à niveau
neutre (pas transferts). Le signal accumulé dans l'ancien contexte ne transite
pas comme une importance acquise dans le nouveau.

Sans modèle, sans GPU : base temporaire, périmètre réel, zéro appel d'outil.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.db.models import (
    Artifact,
    ArtifactLink,
    Base,
    Blob,
    Conversation,
    MemoryEntry,
    MemoryUsage,
)
from backend.services import espaces as E
from backend.services import memoire as M


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """Session sur base temporaire, injectée comme SessionLocal du projet."""
    engine = create_engine(f"sqlite:///{tmp_path / 'remise_neutre.db'}")
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    monkeypatch.setattr("backend.db.database.SessionLocal", TestSession)
    s = TestSession()
    try:
        yield s
    finally:
        s.close()


def _artifact(s, art_id: str, name: str = "doc.txt", body: bytes = b"contenu") -> str:
    """Crée l'artefact s'il n'existe pas — idempotent, pour qu'un test puisse
    appeler plusieurs fois `_source` sans violer l'unicité du blob."""
    if s.query(Artifact).filter(Artifact.id == art_id).first() is None:
        s.add(
            Blob(
                id=f"sha_{art_id}",
                sha256=f"sha_{art_id}",
                relative_path=f"blobs/sha_{art_id}.bin",
                size_bytes=len(body),
                mime_detected="text/plain",
                state="available",
            )
        )
        s.add(
            Artifact(
                id=art_id,
                blob_id=f"sha_{art_id}",
                kind="text",
                original_filename=name,
                display_name=name,
            )
        )
        s.commit()
    return art_id


def _conv(s, conv_id: str, ws: str | None = None) -> str:
    s.add(Conversation(id=conv_id, title="t", mode="chat", workspace_id=ws))
    s.commit()
    return conv_id


def _link(s, art_id: str, kind: str, scope_id: str) -> None:
    s.add(
        ArtifactLink(
            id=f"lnk_{art_id}_{kind}_{scope_id}",
            artifact_id=art_id,
            scope_kind=kind,
            scope_id=scope_id,
            role="shared",
        )
    )
    s.commit()


def _partage_source(s, ws_id: str, conv_id: str) -> str:
    """Source réellement partagée dans l'espace : le parcours complet."""
    art_id = f"src_{ws_id}"
    _artifact(s, art_id, name=f"{art_id}.txt")
    _conv(s, conv_id, ws=ws_id)
    _link(s, art_id, "conversation", conv_id)
    E.share_artifact(s, ws_id, art_id, from_conv_id=conv_id)
    return art_id


def _fiche(
    s,
    ws_id: str,
    key: str,
    contenu: str,
    *,
    sources: list[str],
    statut: str = "accepted",
) -> str:
    """Fiche via le vrai service : proposition puis statut demandé."""
    out = M.propose(
        s,
        ws_id,
        contenu,
        source_artifact_ids=list(sources),
        origin_context="conversation de travail",
        promotion_reasons=[{"kind": "outil", "detail": "usage répété"}],
        idempotency_key=key,
    )
    entry_id = out["entry_id"]
    if statut == "accepted":
        entry_id = M.accept(s, entry_id)["entry_id"]
    elif statut == "rejected":
        entry_id = M.reject(s, entry_id)["entry_id"]
    elif statut == "stale":
        entry_id = M.mark_stale(s, entry_id)["entry_id"]
    return entry_id


# ---------------------------------------------------------------------------
# memoire.remettre_a_neutre_signaux — le reset du signal d'importance
# ---------------------------------------------------------------------------


def test_remettre_a_neutre_signaux_retire_le_signal_et_zero_le_compteur(db):
    """Un usage réel est remis à neutre : usages supprimés, compteur à zéro."""
    ws = E.create_workspace(db, "Signal").id
    source = _partage_source(db, ws, "conv_mvt")
    eid = _fiche(
        db, ws, "k1", "Le protocole de sauvegarde utilise SQLite.", sources=[source]
    )

    assert M.record_usage(db, eid, "msg_mvt_1")["counted"] is True
    entry = db.query(MemoryEntry).filter(MemoryEntry.id == eid).first()
    assert int(entry.usage_count or 0) == 1
    assert db.query(MemoryUsage).filter(MemoryUsage.entry_id == eid).count() == 1

    out = M.remettre_a_neutre_signaux(db, ws, [source])

    assert out["workspace_id"] == ws
    assert out["artifacts"] == [source]
    assert out["entrees"] == 1
    assert out["signaux_supprimes"] == 1
    courante = db.query(MemoryEntry).filter(MemoryEntry.id == eid).first()
    assert int(courante.usage_count or 0) == 0
    assert courante.last_used_at is None
    assert db.query(MemoryUsage).filter(MemoryUsage.entry_id == eid).count() == 0


def test_remettre_a_neutre_signaux_sans_contribution_retourne_un_restant_neutre(db):
    """Sans contribution ciblée, le reset est un no-op : rien à neutraliser."""
    ws = E.create_workspace(db, "Signal").id
    assert M.remettre_a_neutre_signaux(db, ws) == {
        "workspace_id": ws,
        "artifacts": [],
        "entrees": 0,
        "signaux_supprimes": 0,
    }
    assert M.remettre_a_neutre(db, ws, [])["entrees"] == 0
    assert M.remettre_a_neutre_usage(db, ws, None)["signaux_supprimes"] == 0


def test_remettre_a_neutre_signaux_ne_touche_que_les_fiches_citantes(db):
    """Seules les fiches citant une contribution en départ sont remises à neutre."""
    ws = E.create_workspace(db, "Signal").id
    source_a = _partage_source(db, ws, "conv_mvt")
    source_b = _artifact(db, "art_seconde")
    _conv(db, "conv_seconde", ws=ws)
    _link(db, "art_seconde", "conversation", "conv_seconde")
    E.share_artifact(db, ws, "art_seconde", from_conv_id="conv_seconde")

    fiche_a = _fiche(
        db, ws, "k1", "Le protocole de sauvegarde utilise SQLite.", sources=[source_a]
    )
    fiche_b = _fiche(
        db, ws, "k2", "Le serveur de fichiers réplique le planning.", sources=[source_b]
    )
    assert M.record_usage(db, fiche_a, "msg_mvt_1")["counted"] is True
    assert M.record_usage(db, fiche_b, "msg_mvt_2")["counted"] is True

    out = M.remettre_a_neutre_signaux(db, ws, [source_a])

    assert out["entrees"] == 1
    assert out["artifacts"] == [source_a]
    courante_a = db.query(MemoryEntry).filter(MemoryEntry.id == fiche_a).first()
    assert int(courante_a.usage_count or 0) == 0
    assert db.query(MemoryUsage).filter(MemoryUsage.entry_id == fiche_a).count() == 0
    courante_b = db.query(MemoryEntry).filter(MemoryEntry.id == fiche_b).first()
    assert int(courante_b.usage_count or 0) == 1
    assert db.query(MemoryUsage).filter(MemoryUsage.entry_id == fiche_b).count() == 1


# ---------------------------------------------------------------------------
# Déplacement — le reset est appliqué par `request_move` à l'accord
# ---------------------------------------------------------------------------


def test_le_deplacement_remet_a_neutre_le_signal_des_contributions(db):
    """§7.3 — à l'accord, le signal des contributions déplacées repart de zéro."""
    src = E.create_workspace(db, "Source")
    dst = E.create_workspace(db, "Destination")
    source = _partage_source(db, src.id, "conv_1")
    eid = _fiche(db, src.id, "k1", "Fait cité depuis la discussion.", sources=[source])
    assert M.record_usage(db, eid, "msg_mvt_1")["counted"] is True

    out = E.request_move(db, "conv_1", dst.id, confirm=True)

    assert out["applied"] is True
    assert out["signaux_remis_a_neutre"]["entrees"] == 1
    assert out["signaux_remis_a_neutre"]["signaux_supprimes"] == 1
    courante = db.query(MemoryEntry).filter(MemoryEntry.id == eid).first()
    assert int(courante.usage_count or 0) == 0
    assert db.query(MemoryUsage).filter(MemoryUsage.entry_id == eid).count() == 0
    assert E.conversation_workspace(db, "conv_1") == dst.id
