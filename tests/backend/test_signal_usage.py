"""Tests du signal d'importance (recherche_local.md §6.4) — task 5.6.

Le signal naît d'un usage RÉEL : un outil de recherche qui réussit a sollicité
une ressource, et c'est ce retour qui pèse sur la fiche liée. Sans wiring, les
consultations qui n'aboutissaient pas à une preuve retenue étaient silencieuses —
une recherche qui trouve n'incrémentait jamais l'usage.

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
from backend.inference import loop as L
from backend.services import espaces as E
from backend.services import memoire as M


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """Session sur base temporaire, injectée comme SessionLocal du projet."""
    engine = create_engine(f"sqlite:///{tmp_path / 'signal_usage.db'}")
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
    """Source réellement partagée dans l'espace : la provenance par défaut.

    Même parcours que les autres tests : import dans le chat, PUIS partage vers
    l'espace — jamais de source énoncée sans lien.
    """
    art_id = f"src_{ws_id}"
    _artifact(s, art_id, name=f"{art_id}.txt")
    _conv(s, conv_id, ws=ws_id)
    _link(s, art_id, "conversation", conv_id)
    E.share_artifact(s, ws_id, art_id, from_conv_id=conv_id)
    return art_id


def _fiche(
    db,
    ws_id: str,
    key: str,
    contenu: str,
    *,
    sources: list[str],
    statut: str = "accepted",
) -> str:
    """Fiche via le vrai service : proposition puis statut demandé."""
    out = M.propose(
        db,
        ws_id,
        contenu,
        source_artifact_ids=list(sources),
        origin_context="conversation de travail",
        promotion_reasons=[{"kind": "outil", "detail": "usage répété"}],
        idempotency_key=key,
    )
    entry_id = out["entry_id"]
    if statut == "accepted":
        entry_id = M.accept(db, entry_id)["entry_id"]
    elif statut == "rejected":
        entry_id = M.reject(db, entry_id)["entry_id"]
    elif statut == "stale":
        entry_id = M.mark_stale(db, entry_id)["entry_id"]
    return entry_id


# ---------------------------------------------------------------------------
# memoire.enregistrer_usages — l'émission du signal
# ---------------------------------------------------------------------------


def test_enregistrer_usages_compte_la_fiche_couverte(db):
    """Une ressource rendue par un outil de recherche incrémente la fiche couverte."""
    ws = E.create_workspace(db, "Signal").id
    source = _partage_source(db, ws, "conv_signal")
    eid = _fiche(
        db, ws, "k1", "Le protocole de sauvegarde utilise SQLite.", sources=[source]
    )

    comptes = M.enregistrer_usages(
        db, ws, [source], evenement="retour", response_id="msg_signal"
    )

    assert comptes == [eid]
    entry = db.query(MemoryEntry).filter(MemoryEntry.id == eid).first()
    assert entry is not None
    assert int(entry.usage_count or 0) == 1
    usage = (
        db.query(MemoryUsage)
        .filter(MemoryUsage.entry_id == eid, MemoryUsage.response_id == "msg_signal")
        .first()
    )
    assert usage is not None
    assert usage.usage_key == M.usage_key(
        "msg_signal", eid, int(entry.version), "retour"
    )


def test_enregistrer_usages_deux_evenements_sont_comptes_séparément(db):
    """Deux natures de sollicitation, deux compteurs : une interrogation ne
    compense jamais un simple retour."""
    ws = E.create_workspace(db, "Signal").id
    source = _partage_source(db, ws, "conv_signal")
    eid = _fiche(
        db, ws, "k2", "Le serveur de fichiers réplique le planning.", sources=[source]
    )

    assert M.enregistrer_usages(
        db, ws, [source], evenement="retour", response_id="msg_signal"
    ) == [eid]
    assert M.enregistrer_usages(
        db, ws, [source], evenement="interrogation", response_id="msg_signal"
    ) == [eid]

    entry = db.query(MemoryEntry).filter(MemoryEntry.id == eid).first()
    assert int(entry.usage_count or 0) == 2

    # La RELANCE de la même sollicitation reste une seule et même chose.
    assert (
        M.enregistrer_usages(
            db, ws, [source], evenement="retour", response_id="msg_signal"
        )
        == []
    )
    entry = db.query(MemoryEntry).filter(MemoryEntry.id == eid).first()
    assert int(entry.usage_count or 0) == 2


def test_enregistrer_usages_ignore_rejet_et_périmètre_vide(db):
    """Un rejet ne pèse pas et un périmètre sans fiche ne crache pas : le signal
    ne compte que les usages réels, jamais le bruit ambiant."""
    ws = E.create_workspace(db, "Signal").id
    source = _partage_source(db, ws, "conv_signal")
    _fiche(
        db,
        ws,
        "k3",
        "Le backup nocturne est incremental.",
        sources=[source],
        statut="rejected",
    )

    assert (
        M.enregistrer_usages(
            db, ws, [source], evenement="retour", response_id="msg_signal"
        )
        == []
    )

    assert (
        M.enregistrer_usages(
            db, ws, ["src_inconnu"], evenement="retour", response_id="msg_autre"
        )
        == []
    )
    assert (
        M.enregistrer_usages(
            db, "ws_inexistant", [source], evenement="retour", response_id="msg_autre"
        )
        == []
    )


# ---------------------------------------------------------------------------
# loop — la nature du signal, la résolution des ressources, l'émission
# ---------------------------------------------------------------------------


def test_evenement_usage_par_nature_de_sollicitation():
    """`retour` pour un résultat rendu, `interrogation` pour une vraie question :
    deux natures distinctes ne partagent jamais un compteur."""
    assert L._evenement_usage("find_files") == L.EVENEMENT_SIGNAL_RETOUR
    assert L._evenement_usage("search_info") == L.EVENEMENT_SIGNAL_RETOUR
    assert L._evenement_usage("ask_files") == L.EVENEMENT_SIGNAL_INTERROGATION
    assert L._evenement_usage("web_research") is None
    assert L._evenement_usage("generate_image") is None


def test_ressources_pour_signal_resolvent_le_doc_public(monkeypatch, db):
    """Les `doc_N` du résultat sont résolus vers l'artefact interne ; une
    ressource sans identité exploitable ne compte pas."""
    ressources = L._ressources_pour_signal(
        db,
        "conv_signal",
        "find_files",
        {"query": "protocole"},
        {"ok": True, "public_ids": ["doc_1"]},
    )
    assert ressources == ["doc_1"]

    monkeypatch.setattr(
        "backend.search.docrefs.resolve_doc_label",
        lambda _db, _conv_id, _doc: "art_sig",
    )
    assert L._ressources_pour_signal(
        db,
        "conv_signal",
        "ask_files",
        {"questions": ["x"], "resource_ids": ["doc_1"]},
        {"ok": True, "public_ids": ["doc_1"]},
    ) == ["art_sig"]
    assert (
        L._ressources_pour_signal(
            db,
            "conv_signal",
            "find_files",
            {"query": "protocole"},
            {"ok": True, "public_ids": []},
        )
        == []
    )


def test_signal_importance_emet_le_signal_auxoutils_de_recherche(monkeypatch, db):
    """Le wiring du signal : les succès des 3 outils de recherche émettent un
    usage réel ; l'échec et l'inconnu ne font pas de signal."""
    ws = E.create_workspace(db, "Signal").id
    monkeypatch.setattr(
        "backend.services.espaces.conversation_workspace", lambda _db, conv_id: ws
    )
    appels: list[dict] = []

    def _fake_enregistrer_usages(
        _db,
        workspace_id: str,
        artifact_ids: list[str],
        *,
        evenement: str,
        response_id: str,
    ):
        appels.append(
            {
                "workspace_id": workspace_id,
                "artifact_ids": list(artifact_ids),
                "evenement": evenement,
                "response_id": response_id,
            }
        )
        return []

    monkeypatch.setattr(
        "backend.services.memoire.enregistrer_usages", _fake_enregistrer_usages
    )

    L._signal_importance(
        "find_files",
        {"query": "protocole"},
        {"ok": True, "public_ids": ["doc_1"]},
        "conv_signal",
        "call_42",
    )
    assert appels == [
        {
            "workspace_id": ws,
            "artifact_ids": ["doc_1"],
            "evenement": L.EVENEMENT_SIGNAL_RETOUR,
            "response_id": "call_42",
        }
    ]

    monkeypatch.setattr(
        "backend.search.docrefs.resolve_doc_label",
        lambda _db, _conv_id, _doc: "art_sig",
    )
    L._signal_importance(
        "ask_files",
        {"questions": ["x"], "resource_ids": ["doc_1"]},
        {"ok": True, "public_ids": ["doc_1"]},
        "conv_signal",
        "call_43",
    )
    assert appels[-1]["evenement"] == L.EVENEMENT_SIGNAL_INTERROGATION
    assert appels[-1]["artifact_ids"] == ["art_sig"]

    L._signal_importance(
        "find_files",
        {"query": "protocole"},
        {"ok": False, "error": "boom"},
        "conv_signal",
        "call_44",
    )
    assert len(appels) == 2

    L._signal_importance(
        "web_research", {"query": "protocole"}, {"ok": True}, "conv_signal", "call_45"
    )
    assert len(appels) == 2


def test_signal_importance_ne_propage_pas_les_erreurs(monkeypatch, db):
    """Le signal est un plus : son échec ne fait jamais échouer le tour."""

    def _explose(*args, **kwargs):
        raise RuntimeError("base indisponible")

    monkeypatch.setattr("backend.services.memoire.enregistrer_usages", _explose)

    L._signal_importance(
        "find_files",
        {"query": "protocole"},
        {"ok": True, "public_ids": ["doc_1"]},
        "conv_signal",
        "call_46",
    )
