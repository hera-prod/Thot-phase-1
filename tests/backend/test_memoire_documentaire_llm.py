"""Extraction documentaire structurée par LLM (Phase 8.4 / 8.8).

Ces tests ne chargent AUCUN modèle : un faux runtime rend des appels d'outil
OpenAI. Ils verrouillent les propriétés qui comptent :

- un schéma valide écrit des faits `method="llm"` avec une provenance complète ;
- toute sortie invalide (champ en trop, citation courte, chunk inconnu, citation
  hors du fragment désigné) retombe sur le déterministe ;
- une contention GPU diffère sans incrémenter `attempts` ;
- un bail perdu pendant l'appel LLM n'écrit rien ;
- le remplacement versionné et la correction utilisateur tiennent aussi pour
  les faits LLM ;
- la réservation mémoire est non bloquante (`timeout=0`).
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.db.models import (
    Artifact,
    ArtifactChunk,
    ArtifactLink,
    Base,
    Blob,
    MemoryDocumentExtraction,
    MemoryEntry,
    Observation,
    Workspace,
)
from backend.inference.resource_coordinator import ResourceBusy
from backend.prompts import document_facts as P
from backend.search import cards as C
from backend.search import chunks as K
from backend.services import activity
from backend.services import document_extraction_llm as E
from backend.services import memoire as M
from backend.services import memoire_documentaire as D


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture(autouse=True)
def _etat_propre(monkeypatch):
    activity.marquer_tour_actif(False)
    D.reinitialiser_compteurs_enqueue()
    D.definir_version_extracteur_active(D.VERSION_EXTRACTEUR)
    monkeypatch.setattr(D, "EXTRACTEUR_STRUCTURE", None)
    yield
    D.definir_version_extracteur_active(D.VERSION_EXTRACTEUR)
    activity.marquer_tour_actif(False)
    with activity._OWNER_LOCK:
        activity._OWNER = None
    try:
        activity._RESERVATION_LOCK.release()
    except RuntimeError:
        pass


@pytest.fixture()
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'doc_llm.db'}")
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    monkeypatch.setattr("backend.db.database.SessionLocal", TestSession)
    monkeypatch.setattr(K, "_indexer_fragment", lambda *_a, **_k: True)
    s = TestSession()
    try:
        K.ensure_chunk_tables(s)
        from backend.search import fts

        fts.ensure_fts(s)
        yield s
    finally:
        s.close()


def _workspace(db, workspace_id: str = "ws_llm") -> str:
    db.add(Workspace(id=workspace_id, name="Espace LLM"))
    db.commit()
    return workspace_id


def _artifact(
    db,
    artifact_id: str = "art_llm",
    *,
    state: str = "extrait",
    kind: str = "text",
) -> str:
    db.add(
        Blob(
            id=f"sha_{artifact_id}",
            sha256=f"sha_{artifact_id}",
            relative_path=f"blobs/{artifact_id}.bin",
            size_bytes=100,
            mime_detected="text/plain",
            state="available",
        )
    )
    db.add(
        Artifact(
            id=artifact_id,
            blob_id=f"sha_{artifact_id}",
            kind=kind,
            original_filename=f"{artifact_id}.txt",
            display_name=f"{artifact_id}.txt",
            ingest_state=state,
        )
    )
    db.commit()
    return artifact_id


def _lien(db, workspace_id: str, artifact_id: str) -> None:
    db.add(
        ArtifactLink(
            id=f"lnk_{workspace_id}_{artifact_id}",
            artifact_id=artifact_id,
            scope_kind="workspace",
            scope_id=workspace_id,
            role="shared",
        )
    )
    db.commit()


def _observation(
    db,
    artifact_id: str,
    payload: str,
    *,
    observation_id: str = "obs_llm_1",
) -> Observation:
    obs = Observation(
        id=observation_id,
        artifact_id=artifact_id,
        tool_name="attachment_ingest",
        tool_version="test/1",
        status="completed",
        payload_ref=payload,
        result_json="{}",
    )
    db.add(obs)
    db.commit()
    return obs


def _chunks(db, artifact_id: str, texte: str, workspace_id: str = "ws_llm") -> int:
    art = db.query(Artifact).filter(Artifact.id == artifact_id).first()
    C.ensure_card_on_import(db, art)
    n = K.ensure_chunks_for_artifact(db, art, texte, [("workspace", workspace_id)])
    db.commit()
    return n


def _premier_chunk(db, artifact_id: str) -> ArtifactChunk:
    return (
        db.query(ArtifactChunk)
        .filter(ArtifactChunk.artifact_id == artifact_id)
        .order_by(ArtifactChunk.ordinal.asc())
        .first()
    )


def _ledger(
    db,
    *,
    artifact_id: str = "art_llm",
    workspace_id: str = "ws_llm",
    observation_id: str | None = "obs_llm_1",
    status: str = "pending",
    attempts: int = 0,
    extractor_version: str = D.VERSION_EXTRACTEUR,
) -> MemoryDocumentExtraction:
    ligne = MemoryDocumentExtraction(
        id=f"mdx_{status}_{artifact_id}_{attempts}_{extractor_version}",
        workspace_id=workspace_id,
        artifact_id=artifact_id,
        observation_id=observation_id,
        extractor_version=extractor_version,
        status=status,
        attempts=attempts,
    )
    db.add(ligne)
    db.commit()
    return ligne


def _fait_llm(
    *,
    content: str,
    quote: str,
    chunk_id: str,
    entry_key: str = "doc:ws_llm:art_llm:abc",
    artifact_id: str = "art_llm",
    observation_id: str = "obs_llm_1",
    workspace_id: str = "ws_llm",
    method: str = "llm",
) -> D.FaitDocument:
    return D.FaitDocument(
        content=content,
        quote=quote,
        category="connaissances_de_travail",
        affirmation_status="observation",
        entry_key=entry_key,
        origin_context=(
            f"document:{artifact_id}:observation:{observation_id}:chunk:{chunk_id}"
        ),
        evidence_ids=[observation_id, chunk_id],
        source_artifact_ids=[artifact_id],
        observation_id=observation_id,
        chunk_id=chunk_id,
        chunk_ordinal=0,
        card_revision=1,
        method=method,
        extractor_version=E.VERSION_EXTRACTEUR_LLM,
    )


def _payload_faits(faits: list[dict], *, nom: str = E.NOM_OUTIL) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "function": {
                                "name": nom,
                                "arguments": json.dumps({"faits": faits}),
                            },
                        }
                    ]
                }
            }
        ]
    }


class FauxRuntime:
    """Runtime minimal : rend l'appel d'outil demandé, sans llama_cpp."""

    def __init__(
        self,
        faits: list[dict] | None = None,
        *,
        payload: dict | None = None,
        erreur: Exception | None = None,
    ) -> None:
        self.faits = faits or []
        self.payload = payload
        self.erreur = erreur
        self.requetes: list = []

    def complete_chat(self, request):
        self.requests = getattr(self, "requests", [])
        self.requetes.append(request)
        if self.erreur is not None:
            raise self.erreur
        payload = (
            self.payload if self.payload is not None else _payload_faits(self.faits)
        )
        return SimpleNamespace(raw=payload)


def _extraire_avec(
    db,
    runtime,
    *,
    texte,
    observation_id="obs_llm_1",
    revision=1,
    version=E.VERSION_EXTRACTEUR_LLM,
):
    return D.extraire_faits_document(
        db,
        artifact_id="art_llm",
        workspace_id="ws_llm",
        texte=texte,
        observation_id=observation_id,
        extractor_version=version,
        card_revision=revision,
        extracteur_structure=lambda **kwargs: E.extraire_faits_llm(
            runtime=runtime, **kwargs
        ),
    )


# ---------------------------------------------------------------------------
# 1. Schéma valide
# ---------------------------------------------------------------------------


def test_schema_valide_ecrit_des_faits_llm_avec_provenance(db):
    ws = _workspace(db)
    art = _artifact(db)
    _lien(db, ws, art)
    texte = "Le serveur utilise SQLite comme base de données depuis 2024."
    _chunks(db, art, texte)
    chunk = _premier_chunk(db, art)

    runtime = FauxRuntime(
        [
            {
                "content": "Le serveur utilise SQLite comme base de données.",
                "category": "connaissances_de_travail",
                "affirmation_status": "observation",
                "quote": texte,
                "chunk_id": chunk.id,
            }
        ]
    )
    faits = _extraire_avec(db, runtime, texte=texte)

    assert len(faits) == 1
    fait = faits[0]
    assert fait.method == "llm"
    assert fait.extractor_version == E.VERSION_EXTRACTEUR_LLM
    assert fait.chunk_id == chunk.id
    assert fait.origin_context == (
        f"document:{art}:observation:obs_llm_1:chunk:{chunk.id}"
    )
    assert chunk.id in fait.evidence_ids
    assert "obs_llm_1" in fait.evidence_ids
    assert len(runtime.requetes) == 1
    assert runtime.requetes[0].tool_choice == {
        "type": "function",
        "function": {"name": E.NOM_OUTIL},
    }
    assert runtime.requetes[0].timeout_s == E.TIMEOUT_LLM_S
    assert runtime.requetes[0].max_tokens == E.MAX_TOKENS_LLM
    assert 0.0 < E.TIMEOUT_LLM_S < D.BUDGET_SECONDES

    D.appliquer_faits_documentaires(
        db,
        workspace_id=ws,
        artifact_id=art,
        faits=faits,
        observation_id="obs_llm_1",
        extractor_version=E.VERSION_EXTRACTEUR_LLM,
    )
    entry = db.query(MemoryEntry).filter(MemoryEntry.is_current == 1).one()
    assert {
        "kind": "document",
        "method": "llm",
        "version": E.VERSION_EXTRACTEUR_LLM,
    } in (entry.promotion_reasons or [])
    assert entry.source_observation_id == "obs_llm_1"


# ---------------------------------------------------------------------------
# 2. Schéma invalide -> repli déterministe
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mutation",
    [
        {"champ_en_trop": True},
        {"quote": "trop court"},
        {"chunk_id": "chk_inconnu"},
    ],
)
def test_schema_invalide_replie_sur_deterministe_sans_fait_invalide(db, mutation):
    ws = _workspace(db)
    art = _artifact(db)
    _lien(db, ws, art)
    texte = "Le serveur utilise SQLite comme base de données depuis 2024."
    _chunks(db, art, texte)
    chunk = _premier_chunk(db, art)

    fait = {
        "content": "Le serveur utilise SQLite comme base de données.",
        "category": "connaissances_de_travail",
        "affirmation_status": "observation",
        "quote": texte,
        "chunk_id": chunk.id,
    }
    fait.update(mutation)
    runtime = FauxRuntime([fait])
    faits = _extraire_avec(db, runtime, texte=texte)

    assert len(runtime.requetes) == 1
    assert len(faits) == 1
    assert faits[0].method == "deterministe"
    assert faits[0].content == texte
    assert faits[0].chunk_id == chunk.id


def test_un_fait_invalide_ne_rejette_pas_les_faits_valides(db):
    """La validation est PAR FAIT : un item court ne jette pas les autres."""
    ws = _workspace(db)
    art = _artifact(db)
    _lien(db, ws, art)
    texte = "Le serveur utilise SQLite comme base de données depuis 2024."
    _chunks(db, art, texte)
    chunk = _premier_chunk(db, art)

    valide = {
        "content": "Le serveur utilise SQLite comme base de données.",
        "category": "connaissances_de_travail",
        "affirmation_status": "observation",
        "quote": texte,
        "chunk_id": chunk.id,
    }
    invalide = {
        "content": "Un fait dont la citation est trop courte.",
        "category": "connaissances_de_travail",
        "affirmation_status": "observation",
        "quote": "court",
        "chunk_id": chunk.id,
    }
    runtime = FauxRuntime([valide, invalide])
    faits = _extraire_avec(db, runtime, texte=texte)

    assert len(runtime.requetes) == 1
    assert len(faits) == 1
    assert faits[0].method == "llm"
    assert faits[0].content == "Le serveur utilise SQLite comme base de données."
    assert faits[0].quote == texte


def test_reponse_tronquee_replie_sur_deterministe(db):
    """`finish_reason="length"` invalide TOUTE la sortie, même bien formée."""
    ws = _workspace(db)
    art = _artifact(db)
    _lien(db, ws, art)
    texte = "Le serveur utilise SQLite comme base de données depuis 2024."
    _chunks(db, art, texte)
    chunk = _premier_chunk(db, art)

    payload = _payload_faits(
        [
            {
                "content": "Le serveur utilise SQLite comme base de données.",
                "category": "connaissances_de_travail",
                "affirmation_status": "observation",
                "quote": texte,
                "chunk_id": chunk.id,
            }
        ]
    )
    payload["choices"][0]["finish_reason"] = "length"
    runtime = FauxRuntime(payload=payload)

    faits = _extraire_avec(db, runtime, texte=texte)

    assert len(runtime.requetes) == 1
    assert len(faits) == 1
    assert faits[0].method == "deterministe"


def test_enveloppe_de_plus_de_cinq_faits_est_invalidee(db):
    """La liste hors `maxItems=5` n'est jamais tronquée puis acceptée."""
    ws = _workspace(db)
    art = _artifact(db)
    _lien(db, ws, art)
    texte = "Le serveur utilise SQLite comme base de données depuis 2024."
    _chunks(db, art, texte)
    chunk = _premier_chunk(db, art)
    fait = {
        "content": "Le serveur utilise SQLite comme base de données.",
        "category": "connaissances_de_travail",
        "affirmation_status": "observation",
        "quote": texte,
        "chunk_id": chunk.id,
    }

    runtime = FauxRuntime([fait] * (P.MAX_FAITS_LLM + 1))
    faits = _extraire_avec(db, runtime, texte=texte)

    assert len(runtime.requetes) == 1
    assert len(faits) == 1
    assert faits[0].method == "deterministe"


# ---------------------------------------------------------------------------
# 3. Citation hors du fragment désigné -> rejet
# ---------------------------------------------------------------------------


def test_citation_absente_du_chunk_designe_est_rejetee(db):
    ws = _workspace(db)
    art = _artifact(db)
    _lien(db, ws, art)
    # Deux fragments réels, aucun verbe déclaratif : le déterminisme ne peut
    # pas masquer un mauvais appariement citation/fragment.
    texte = (
        "Base PostgreSQL 2025 pour un autre projet. " * 100
        + "Archive froide 2026 sur disque distant chaque nuit. " * 100
    )
    _chunks(db, art, texte)
    fragments = (
        db.query(ArtifactChunk)
        .filter(ArtifactChunk.artifact_id == art)
        .order_by(ArtifactChunk.ordinal.asc())
        .all()
    )
    assert len(fragments) >= 2
    premier, dernier = fragments[0], fragments[-1]
    citation_du_dernier = dernier.text[:80].strip()

    runtime = FauxRuntime(
        [
            {
                "content": "Une base PostgreSQL pour un autre projet.",
                "category": "connaissances_de_travail",
                "affirmation_status": "observation",
                "quote": citation_du_dernier,
                "chunk_id": premier.id,
            }
        ]
    )
    faits = _extraire_avec(db, runtime, texte=texte)

    assert len(runtime.requetes) == 1
    assert faits == []


# ---------------------------------------------------------------------------
# 4. Contention GPU -> deferred, pas d'attempts
# ---------------------------------------------------------------------------


def test_resource_busy_differe_sans_attempts(db, monkeypatch):
    ws = _workspace(db)
    art = _artifact(db)
    _lien(db, ws, art)
    texte = "Le serveur utilise SQLite comme base de données depuis 2024."
    obs = _observation(db, art, texte)
    _chunks(db, art, texte)
    _ledger(db, artifact_id=art, workspace_id=ws, observation_id=obs.id)

    def occupe(**_kwargs):
        raise ResourceBusy("GPU occupé")

    monkeypatch.setattr(D, "EXTRACTEUR_STRUCTURE", occupe)
    compteurs = D.executer_lot_documentaire()

    ligne = db.query(MemoryDocumentExtraction).one()
    assert compteurs["deferred"] == 1
    assert ligne.status == "deferred"
    assert ligne.attempts == 0
    assert ligne.deferrals == 1


# ---------------------------------------------------------------------------
# 5. Panne runtime -> repli déterministe
# ---------------------------------------------------------------------------


def test_timeout_runtime_replie_sur_deterministe(db):
    ws = _workspace(db)
    art = _artifact(db)
    _lien(db, ws, art)
    texte = "Le serveur utilise SQLite comme base de données depuis 2024."
    _chunks(db, art, texte)

    runtime = FauxRuntime(erreur=TimeoutError("RUNTIME_TIMEOUT"))
    faits = _extraire_avec(db, runtime, texte=texte)

    assert len(runtime.requetes) == 1
    assert len(faits) == 1
    assert faits[0].method == "deterministe"


# ---------------------------------------------------------------------------
# 6. Bail perdu pendant l'appel LLM -> aucune écriture
# ---------------------------------------------------------------------------


def test_bail_perdu_pendant_l_appel_llm_aucune_ecriture(db, monkeypatch):
    ws = _workspace(db)
    art = _artifact(db)
    _lien(db, ws, art)
    texte = "Le serveur utilise SQLite comme base de données depuis 2024."
    obs = _observation(db, art, texte)
    _chunks(db, art, texte)
    ligne = _ledger(db, artifact_id=art, workspace_id=ws, observation_id=obs.id)
    chunk = _premier_chunk(db, art)

    def extracteur(**kwargs):
        # Un autre worker reprend le bail pendant l'appel LLM.
        db.query(MemoryDocumentExtraction).filter_by(id=ligne.id).update(
            {"claim_token": "token_du_voleur", "claimed_at": _now()}
        )
        db.commit()
        return [
            _fait_llm(
                content="Le serveur utilise SQLite comme base de données.",
                quote=texte,
                chunk_id=chunk.id,
            )
        ]

    monkeypatch.setattr(D, "EXTRACTEUR_STRUCTURE", extracteur)
    compteurs = D.executer_lot_documentaire()

    assert compteurs["bail_perdu"] == 1
    assert db.query(MemoryEntry).count() == 0


# ---------------------------------------------------------------------------
# 7-9. Remplacement versionné avec des faits LLM
# ---------------------------------------------------------------------------


def test_cle_identique_similarite_rompue_correction_preservee_candidat(db):
    ws = _workspace(db)
    art = _artifact(db)
    _lien(db, ws, art)
    _chunks(db, art, "Le serveur utilise SQLite comme base de données depuis 2024.")
    chunk = _premier_chunk(db, art)

    fait = _fait_llm(
        content="Le serveur utilise SQLite comme base de données.",
        quote="Le serveur utilise SQLite comme base de données depuis 2024.",
        chunk_id=chunk.id,
    )
    db.add(
        MemoryEntry(
            id="mem_user_llm",
            entry_key=fait.entry_key,
            workspace_id=ws,
            version=1,
            is_current=1,
            content="Correction utilisateur : PostgreSQL depuis 2025.",
            status="accepted",
            source_artifact_ids=[art],
            origin_context=fait.origin_context,
            promotion_reasons=[],
            evidence_ids=fait.evidence_ids,
            decision={"actor": "user", "action": "correct", "at": _now().isoformat()},
        )
    )
    db.commit()

    D.appliquer_faits_documentaires(
        db,
        workspace_id=ws,
        artifact_id=art,
        faits=[fait],
        observation_id="obs_llm_1",
        extractor_version=E.VERSION_EXTRACTEUR_LLM,
    )

    user = (
        db.query(MemoryEntry)
        .filter(MemoryEntry.entry_key == fait.entry_key, MemoryEntry.is_current == 1)
        .one()
    )
    assert user.id == "mem_user_llm"
    assert user.content.startswith("Correction utilisateur")
    candidat = (
        db.query(MemoryEntry)
        .filter(
            MemoryEntry.workspace_id == ws,
            MemoryEntry.status == "candidate",
            MemoryEntry.is_current == 1,
        )
        .one()
    )
    assert candidat.decision["action"] == "document_candidate"


def test_reformulation_llm_non_dupliquee(db):
    ws = _workspace(db)
    art = _artifact(db)
    _lien(db, ws, art)
    _chunks(db, art, "Le serveur utilise SQLite comme base de données depuis 2024.")
    chunk = _premier_chunk(db, art)

    premier = _fait_llm(
        content="Le serveur utilise SQLite comme base de données depuis 2024.",
        quote="Le serveur utilise SQLite comme base de données depuis 2024.",
        chunk_id=chunk.id,
    )
    D.appliquer_faits_documentaires(
        db,
        workspace_id=ws,
        artifact_id=art,
        faits=[premier],
        observation_id="obs_llm_1",
        extractor_version=E.VERSION_EXTRACTEUR_LLM,
    )

    reformule = _fait_llm(
        content="Le serveur utilise SQLite comme base de données depuis 2024 aussi.",
        quote="Le serveur utilise SQLite comme base de données depuis 2024.",
        chunk_id=chunk.id,
        entry_key="doc:ws_llm:art_llm:reformule",
        observation_id="obs_llm_2",
    )
    D.appliquer_faits_documentaires(
        db,
        workspace_id=ws,
        artifact_id=art,
        faits=[reformule],
        observation_id="obs_llm_2",
        extractor_version=E.VERSION_EXTRACTEUR_LLM,
    )

    courantes = db.query(MemoryEntry).filter(MemoryEntry.is_current == 1).all()
    assert len(courantes) == 1
    assert courantes[0].version == 2


def test_fait_llm_non_reproduit_devient_stale(db):
    ws = _workspace(db)
    art = _artifact(db)
    _lien(db, ws, art)
    _chunks(db, art, "Le serveur utilise SQLite comme base de données depuis 2024.")
    chunk = _premier_chunk(db, art)

    premier = _fait_llm(
        content="Le serveur utilise SQLite comme base de données depuis 2024.",
        quote="Le serveur utilise SQLite comme base de données depuis 2024.",
        chunk_id=chunk.id,
    )
    D.appliquer_faits_documentaires(
        db,
        workspace_id=ws,
        artifact_id=art,
        faits=[premier],
        observation_id="obs_llm_1",
        extractor_version=E.VERSION_EXTRACTEUR_LLM,
    )
    autre_chunk = _premier_chunk(db, art)
    autre = _fait_llm(
        content="La sauvegarde exige trois copies chiffrées chaque nuit.",
        quote="Le serveur utilise SQLite comme base de données depuis 2024.",
        chunk_id=autre_chunk.id,
        entry_key="doc:ws_llm:art_llm:autre",
        observation_id="obs_llm_2",
    )
    D.appliquer_faits_documentaires(
        db,
        workspace_id=ws,
        artifact_id=art,
        faits=[autre],
        observation_id="obs_llm_2",
        extractor_version=E.VERSION_EXTRACTEUR_LLM,
    )

    ancienne = (
        db.query(MemoryEntry)
        .filter(
            MemoryEntry.entry_key == premier.entry_key,
            MemoryEntry.is_current == 1,
        )
        .one()
    )
    assert ancienne.status == "stale"
    assert ancienne.stale_reason in (
        D.STALE_REASON_DOCUMENT_MIS_A_JOUR,
        D.STALE_REASON_PREUVE_INVALIDEE,
    )


# ---------------------------------------------------------------------------
# 10. Version d'extracteur différente -> réextraction par le chemin LLM
# ---------------------------------------------------------------------------


def test_version_differente_reamorce_et_reextrait_par_llm(db, monkeypatch):
    ws = _workspace(db)
    art = _artifact(db)
    _lien(db, ws, art)
    texte = "Le serveur utilise SQLite comme base de données depuis 2024."
    obs = _observation(db, art, texte)
    _chunks(db, art, texte)
    _ledger(
        db,
        artifact_id=art,
        workspace_id=ws,
        observation_id=obs.id,
        status="done",
        attempts=1,
        extractor_version="doc-faits-deterministe/0",
    )

    D.backfiller_ledger_documentaire(db, extractor_version=E.VERSION_EXTRACTEUR_LLM)
    db.commit()
    db.expire_all()
    ligne = db.query(MemoryDocumentExtraction).one()
    assert ligne.status == "pending"
    assert ligne.extractor_version == E.VERSION_EXTRACTEUR_LLM

    chunk = _premier_chunk(db, art)
    runtime = FauxRuntime(
        [
            {
                "content": "Le serveur utilise SQLite comme base de données.",
                "category": "connaissances_de_travail",
                "affirmation_status": "observation",
                "quote": texte,
                "chunk_id": chunk.id,
            }
        ]
    )
    monkeypatch.setattr(
        D,
        "EXTRACTEUR_STRUCTURE",
        lambda **kwargs: E.extraire_faits_llm(runtime=runtime, **kwargs),
    )
    compteurs = D.executer_lot_documentaire()

    assert compteurs["done"] == 1, compteurs
    db.expire_all()
    entry = db.query(MemoryEntry).filter(MemoryEntry.is_current == 1).one()
    assert {
        "kind": "document",
        "method": "llm",
        "version": E.VERSION_EXTRACTEUR_LLM,
    } in (entry.promotion_reasons or [])


def test_backfill_sans_argument_utilise_la_version_llm_active(db):
    """Le backfill sans version lit la version ACTIVE, pas un défaut figé."""
    ws = _workspace(db)
    art = _artifact(db)
    _lien(db, ws, art)
    texte = "Le serveur utilise SQLite comme base de données depuis 2024."
    obs = _observation(db, art, texte)
    _chunks(db, art, texte)
    chunk = _premier_chunk(db, art)
    runtime = FauxRuntime(
        [
            {
                "content": "Le serveur utilise SQLite comme base de données.",
                "category": "connaissances_de_travail",
                "affirmation_status": "observation",
                "quote": texte,
                "chunk_id": chunk.id,
            }
        ]
    )
    E.installer_extracteur_llm(lambda: runtime)

    compteurs = D.backfiller_ledger_documentaire(db)
    assert compteurs["crees"] == 1
    ligne = db.query(MemoryDocumentExtraction).one()
    assert ligne.status == "pending"
    assert ligne.observation_id == obs.id
    assert ligne.extractor_version == E.VERSION_EXTRACTEUR_LLM

    db.commit()
    compteurs = D.executer_lot_documentaire()
    assert compteurs["done"] == 1, compteurs
    db.expire_all()
    entry = db.query(MemoryEntry).filter(MemoryEntry.is_current == 1).one()
    assert {
        "kind": "document",
        "method": "llm",
        "version": E.VERSION_EXTRACTEUR_LLM,
    } in (entry.promotion_reasons or [])
    assert any(
        raison.get("version") == E.VERSION_EXTRACTEUR_LLM
        for raison in entry.promotion_reasons or []
        if isinstance(raison, dict)
    )
    assert len(runtime.requetes) == 1


def test_backfill_rearme_les_done_deterministes_apres_installation_llm(db):
    """Un `done` déterministe est réarmé vers la version LLM active."""
    ws = _workspace(db)
    art = _artifact(db)
    _lien(db, ws, art)
    texte = "Le serveur utilise SQLite comme base de données depuis 2024."
    obs = _observation(db, art, texte)
    _chunks(db, art, texte)
    D._inserer_ligne(
        db,
        workspace_id=ws,
        artifact_id=art,
        observation_id=obs.id,
        extractor_version=D.VERSION_EXTRACTEUR,
        status="done",
        card_revision=1,
    )
    db.commit()

    runtime = FauxRuntime([])
    E.installer_extracteur_llm(lambda: runtime)

    compteurs = D.backfiller_ledger_documentaire(db)
    assert compteurs["crees"] == 0
    db.expire_all()
    lignes = db.query(MemoryDocumentExtraction).all()
    assert len(lignes) == 1
    ligne = lignes[0]
    assert ligne.status == "pending"
    assert ligne.extractor_version == E.VERSION_EXTRACTEUR_LLM
    assert ligne.attempts == 0
    assert ligne.deferrals == 0
    assert ligne.claim_token is None


# ---------------------------------------------------------------------------
# 11. Zéro fait -> rien écrit, statut done
# ---------------------------------------------------------------------------


def test_zero_fait_llm_necrit_rien_et_termine_done(db, monkeypatch):
    ws = _workspace(db)
    art = _artifact(db)
    _lien(db, ws, art)
    texte = "Le serveur utilise SQLite comme base de données depuis 2024."
    obs = _observation(db, art, texte)
    _chunks(db, art, texte)
    _ledger(db, artifact_id=art, workspace_id=ws, observation_id=obs.id)

    runtime = FauxRuntime([])
    monkeypatch.setattr(
        D,
        "EXTRACTEUR_STRUCTURE",
        lambda **kwargs: E.extraire_faits_llm(runtime=runtime, **kwargs),
    )
    compteurs = D.executer_lot_documentaire()

    assert compteurs["done"] == 1
    db.expire_all()
    ligne = db.query(MemoryDocumentExtraction).one()
    assert ligne.status == "done"
    assert db.query(MemoryEntry).count() == 0


# ---------------------------------------------------------------------------
# 12. Document sans texte -> aucun appel LLM
# ---------------------------------------------------------------------------


def test_document_sans_texte_n_appelle_jamais_le_llm(db, monkeypatch):
    ws = _workspace(db)
    art = _artifact(db, state="extrait")
    _lien(db, ws, art)
    obs = _observation(db, art, "")
    _ledger(db, artifact_id=art, workspace_id=ws, observation_id=obs.id)
    appels: list = []

    def extracteur(**kwargs):
        appels.append(kwargs)
        return []

    monkeypatch.setattr(D, "EXTRACTEUR_STRUCTURE", extracteur)
    compteurs = D.executer_lot_documentaire()

    assert appels == []
    assert compteurs["skipped"] == 1
    ligne = db.query(MemoryDocumentExtraction).one()
    assert ligne.status == "skipped"


# ---------------------------------------------------------------------------
# 13. Bornes : 16 000 caractères, max 5 faits, citation >= 12
# ---------------------------------------------------------------------------


def test_bornes_schema_et_entree(db):
    with pytest.raises(Exception):
        P.FaitSchema(
            content="Un fait suffisamment long pour être valide.",
            category="connaissances_de_travail",
            affirmation_status="observation",
            quote="court",
            chunk_id="chk_1",
        )

    fait = {
        "content": "Un fait valide et autonome.",
        "category": "connaissances_de_travail",
        "affirmation_status": "observation",
        "quote": "Une citation assez longue.",
        "chunk_id": "chk_1",
    }
    with pytest.raises(Exception):
        P.FaitsDocumentArgs(faits=[fait] * 6)
    with pytest.raises(Exception):
        P.FaitsDocumentArgs()  # `faits` est requis : l'absence n'est pas « zéro fait »

    fragments = [(f"chk_{i}", i, "x" * 1000) for i in range(20)]
    message = P.construire_message_utilisateur(fragments, max_chars=16_000)
    assert len(message) <= 16_000

    # Le schéma manuel et Pydantic restent alignés : mêmes énumérations, même
    # borne de contenu.
    proprietes = P.EXTRACTION_DOCUMENT_TOOL["function"]["parameters"]["properties"]
    contenu = proprietes["faits"]["items"]["properties"]["content"]
    assert contenu["maxLength"] == P.MAX_CONTENU_FAIT_CHARS
    assert tuple(proprietes["faits"]["items"]["properties"]["category"]["enum"]) == (
        M._MEMORY_CATEGORIES
    )
    assert tuple(
        proprietes["faits"]["items"]["properties"]["affirmation_status"]["enum"]
    ) == (M._MEMORY_AFFIRMATION_STATUSES)


def test_texte_transmis_au_llm_est_borne_a_16000(db):
    ws = _workspace(db)
    art = _artifact(db)
    _lien(db, ws, art)
    texte = "Le serveur utilise SQLite. " * 2000
    _chunks(db, art, texte)
    capture: dict = {}

    def extracteur(**kwargs):
        capture.update(kwargs)
        return []

    D.extraire_faits_document(
        db,
        artifact_id=art,
        workspace_id=ws,
        texte=texte,
        observation_id=None,
        extractor_version=E.VERSION_EXTRACTEUR_LLM,
        card_revision=1,
        extracteur_structure=extracteur,
    )

    assert len(capture["texte"]) == D.MAX_ENTREE_EXTRACTION_CHARS


# ---------------------------------------------------------------------------
# 14. Exclusion respectée
# ---------------------------------------------------------------------------


def test_est_exclu_respecte_pour_les_faits_llm(db):
    ws = _workspace(db)
    art = _artifact(db)
    _lien(db, ws, art)
    _chunks(db, art, "Le serveur utilise SQLite comme base de données depuis 2024.")
    chunk = _premier_chunk(db, art)
    fait = _fait_llm(
        content="Le serveur utilise SQLite comme base de données.",
        quote="Le serveur utilise SQLite comme base de données depuis 2024.",
        chunk_id=chunk.id,
    )
    M.creer_exclusion(db, ws, fait.content, reason="test")

    compteurs = D.appliquer_faits_documentaires(
        db,
        workspace_id=ws,
        artifact_id=art,
        faits=[fait],
        observation_id="obs_llm_1",
        extractor_version=E.VERSION_EXTRACTEUR_LLM,
    )

    assert compteurs["exclus"] == 1
    assert db.query(MemoryEntry).count() == 0


# ---------------------------------------------------------------------------
# 15. Provenance et invalidation après suppression d'artefact
# ---------------------------------------------------------------------------


def test_provenance_llm_invalidee_apres_suppression_artefact(db):
    ws = _workspace(db)
    art = _artifact(db)
    _lien(db, ws, art)
    _chunks(db, art, "Le serveur utilise SQLite comme base de données depuis 2024.")
    chunk = _premier_chunk(db, art)
    fait = _fait_llm(
        content="Le serveur utilise SQLite comme base de données.",
        quote="Le serveur utilise SQLite comme base de données depuis 2024.",
        chunk_id=chunk.id,
    )
    D.appliquer_faits_documentaires(
        db,
        workspace_id=ws,
        artifact_id=art,
        faits=[fait],
        observation_id="obs_llm_1",
        extractor_version=E.VERSION_EXTRACTEUR_LLM,
    )
    entry = db.query(MemoryEntry).filter(MemoryEntry.is_current == 1).one()
    assert entry.source_artifact_ids == [art]
    assert f"document:{art}:" in (entry.origin_context or "")

    artifact = db.query(Artifact).filter(Artifact.id == art).one()
    artifact.deleted_at = _now()
    db.commit()
    M.invalider_artefacts_supprimes(db, [art])

    courante = db.query(MemoryEntry).filter(MemoryEntry.is_current == 1).one()
    assert courante.status == "stale"


# ---------------------------------------------------------------------------
# 16. Le chat n'est jamais bloqué : réservation timeout=0
# ---------------------------------------------------------------------------


def test_reservation_memoire_timeout_zero_et_llm_sans_chat(db, monkeypatch):
    ws = _workspace(db)
    art = _artifact(db)
    _lien(db, ws, art)
    texte = "Le serveur utilise SQLite comme base de données depuis 2024."
    obs = _observation(db, art, texte)
    _chunks(db, art, texte)
    _ledger(db, artifact_id=art, workspace_id=ws, observation_id=obs.id)
    chunk = _premier_chunk(db, art)
    runtime = FauxRuntime(
        [
            {
                "content": "Le serveur utilise SQLite comme base de données.",
                "category": "connaissances_de_travail",
                "affirmation_status": "observation",
                "quote": texte,
                "chunk_id": chunk.id,
            }
        ]
    )
    monkeypatch.setattr(
        D,
        "EXTRACTEUR_STRUCTURE",
        lambda **kwargs: E.extraire_faits_llm(runtime=runtime, **kwargs),
    )

    appels: list[tuple[str, float]] = []
    vrai = activity.try_reserver

    def espion(kind, owner_id, timeout=0.0):
        appels.append((kind, timeout))
        return vrai(kind, owner_id, timeout)

    monkeypatch.setattr(activity, "try_reserver", espion)
    compteurs = D.executer_lot_documentaire()

    assert compteurs["done"] == 1
    assert appels == [("memoire", 0.0)]
    # La réservation mémoire n'est pas un tour de chat : aucun drapeau actif.
    assert activity.tour_actif() is False
    assert activity.etat_activite()["kind"] is None
    assert len(runtime.requetes) == 1


class FauxModele:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.appels: list[dict] = []

    def create_chat_completion(self, **kwargs):
        self.appels.append(kwargs)
        return self.payload


class FauxManager:
    """Expose `use_model` : le seul point d'acquisition GPU attendu."""

    def __init__(self, payload: dict) -> None:
        self.modele = FauxModele(payload)
        self.appels: list[tuple[str, int]] = []
        self.kinds: list[str] = []

    @contextmanager
    def use_model(
        self, name: str, estimated_tokens: int = 0, *, kind: str = "interactive"
    ):
        self.appels.append((name, estimated_tokens))
        self.kinds.append(kind)
        yield self.modele


def test_chemin_manager_utilise_use_model(db):
    ws = _workspace(db)
    art = _artifact(db)
    _lien(db, ws, art)
    texte = "Le serveur utilise SQLite comme base de données depuis 2024."
    _chunks(db, art, texte)
    chunk = _premier_chunk(db, art)
    manager = FauxManager(
        _payload_faits(
            [
                {
                    "content": "Le serveur utilise SQLite comme base de données.",
                    "category": "connaissances_de_travail",
                    "affirmation_status": "observation",
                    "quote": texte,
                    "chunk_id": chunk.id,
                }
            ]
        )
    )

    faits = D.extraire_faits_document(
        db,
        artifact_id=art,
        workspace_id=ws,
        texte=texte,
        observation_id="obs_llm_1",
        extractor_version=E.VERSION_EXTRACTEUR_LLM,
        card_revision=1,
        extracteur_structure=lambda **kwargs: E.extraire_faits_llm(
            manager=manager, **kwargs
        ),
    )

    assert len(faits) == 1 and faits[0].method == "llm"
    assert [nom for nom, _tokens in manager.appels] == [E.MODELE_EXTRACTION]
    assert manager.appels[0][1] > 0
    assert manager.modele.appels[0]["max_tokens"] == E.MAX_TOKENS_LLM


# ---------------------------------------------------------------------------
# 17. Pré-filtrage : phrases candidates, budget ~4 000, FR et EN
# ---------------------------------------------------------------------------


def test_selection_passages_llm_repartit_et_borne_les_candidats():
    fragments = [
        (
            "chk_a",
            0,
            "Un remplissage sans intérêt qui ne dit rien de durable et reste "
            "neutre ici. Le serveur utilise SQLite depuis 2024 avec 3 index.",
        ),
        (
            "chk_b",
            1,
            "Filler neutre encore plus long mais sans aucun signal exploitable "
            "ici. The server uses SQLite as its database since 2024.",
        ),
    ]
    selection = E._selectionner_passages_llm(fragments, max_chars=1_000)

    assert {chunk_id for chunk_id, _ordinal, _texte in selection} == {
        "chk_a",
        "chk_b",
    }
    assert all(E._passage_candidat(texte) for _chunk, _ord, texte in selection)
    assert all("remplissage" not in texte for _c, _o, texte in selection)
    assert all("Filler neutre" not in texte for _c, _o, texte in selection)
    assert sum(len(texte) + 1 for _c, _o, texte in selection) <= 1_000
    # L'ordre source est rétabli : chk_a (ordinal 0) avant chk_b (ordinal 1).
    assert [ordinal for _c, ordinal, _t in selection] == sorted(
        ordinal for _c, ordinal, _t in selection
    )


def test_selection_passages_llm_produit_des_candidats_en_francais_et_en_anglais():
    francais = E._selectionner_passages_llm(
        [("chk_fr", 0, "Le serveur utilise SQLite comme base depuis 2024.")]
    )
    anglais = E._selectionner_passages_llm(
        [("chk_en", 0, "The server uses SQLite as its database since 2024.")]
    )

    assert [chunk for chunk, _o, _t in francais] == ["chk_fr"]
    assert [chunk for chunk, _o, _t in anglais] == ["chk_en"]


def test_prompt_llm_ne_depasse_pas_4000_et_exclut_les_non_candidats(db):
    ws = _workspace(db)
    art = _artifact(db)
    _lien(db, ws, art)
    candidats = " ".join(
        f"Le système numéro {i} utilise SQLite et mesure 2024 unités."
        for i in range(80)
    )
    non_candidat = (
        "Un silence paisible sans chiffre ni nom propre ni verbe daction reste ici."
    )
    texte = f"{candidats} {non_candidat}"
    _chunks(db, art, texte)
    runtime = FauxRuntime([])

    _extraire_avec(db, runtime, texte=texte)

    assert len(runtime.requetes) == 1
    message = runtime.requetes[0].messages[1]["content"]
    assert len(message) <= E.MAX_PASSAGES_LLM_CHARS
    assert "silence paisible" not in message
    assert "[chunk_id=chk_" in message


def test_document_sans_candidat_n_appelle_pas_le_runtime(db):
    ws = _workspace(db)
    art = _artifact(db)
    _lien(db, ws, art)
    texte = "Un silence paisible sans chiffre ni nom propre ni verbe daction reste ici."
    _chunks(db, art, texte)
    runtime = FauxRuntime(
        [
            {
                "content": "Un fait inventé qui ne devrait jamais être demandé.",
                "category": "connaissances_de_travail",
                "affirmation_status": "observation",
                "quote": texte,
                "chunk_id": "chk_inconnu",
            }
        ]
    )

    faits = _extraire_avec(db, runtime, texte=texte)

    assert runtime.requetes == []
    assert faits == []


def test_extraire_faits_llm_rend_none_sans_candidat(db):
    """`None` est le signal du repli, distinct d'une liste vide explicite.

    Le test de bout en bout ne distingue pas `None` de `[]` (le repli
    déterministe transforme les deux). Ici on appelle l'extracteur LLM
    directement : sans phrase candidate, il ne doit ni appeler le runtime ni
    rendre une liste vide.
    """
    ws = _workspace(db)
    art = _artifact(db)
    _lien(db, ws, art)
    texte = "Un silence paisible sans chiffre ni nom propre ni verbe daction reste ici."
    _chunks(db, art, texte)
    runtime = FauxRuntime(
        [
            {
                "content": "Un fait inventé qui ne devrait jamais être demandé.",
                "category": "connaissances_de_travail",
                "affirmation_status": "observation",
                "quote": texte,
                "chunk_id": "chk_inconnu",
            }
        ]
    )

    faits = E.extraire_faits_llm(
        db=db,
        artifact_id=art,
        workspace_id=ws,
        texte=texte,
        observation_id="obs_llm_1",
        extractor_version=E.VERSION_EXTRACTEUR_LLM,
        card_revision=1,
        runtime=runtime,
    )

    assert faits is None
    assert runtime.requetes == []


# ---------------------------------------------------------------------------
# 18. Normalisation du chunk_id renvoyé par le modèle
# ---------------------------------------------------------------------------


def test_chunk_id_avec_suffixe_ordinal_est_normalise_et_accepte(db):
    ws = _workspace(db)
    art = _artifact(db)
    _lien(db, ws, art)
    texte = "Le serveur utilise SQLite comme base de données depuis 2024."
    _chunks(db, art, texte)
    chunk = _premier_chunk(db, art)

    runtime = FauxRuntime(
        [
            {
                "content": "Le serveur utilise SQLite comme base de données.",
                "category": "connaissances_de_travail",
                "affirmation_status": "observation",
                "quote": texte,
                "chunk_id": f'"{chunk.id}  ordinal=3"',
            }
        ]
    )
    faits = _extraire_avec(db, runtime, texte=texte)

    assert len(faits) == 1
    assert faits[0].method == "llm"
    assert faits[0].chunk_id == chunk.id


def test_chunk_id_inconnu_avec_suffixe_reste_rejete(db):
    ws = _workspace(db)
    art = _artifact(db)
    _lien(db, ws, art)
    texte = "Le serveur utilise SQLite comme base de données depuis 2024."
    _chunks(db, art, texte)

    runtime = FauxRuntime(
        [
            {
                "content": "Le serveur utilise SQLite comme base de données.",
                "category": "connaissances_de_travail",
                "affirmation_status": "observation",
                "quote": texte,
                "chunk_id": "chk_inconnu  ordinal=3",
            }
        ]
    )
    faits = _extraire_avec(db, runtime, texte=texte)

    assert len(faits) == 1
    assert faits[0].method == "deterministe"


def test_normaliser_chunk_id_retire_guillemets_et_espaces():
    assert E._normaliser_chunk_id('"chk_abc  ordinal=3"') == "chk_abc"
    assert E._normaliser_chunk_id("chk_abc chunk=xyz") == "chk_abc"
    assert E._normaliser_chunk_id("  chk_ab c  ") == "chk_abc"
    assert E._normaliser_chunk_id("pas_un_chunk") is None


# ---------------------------------------------------------------------------
# 19. Enqueue -> réveil immédiat de la boucle documentaire
# ---------------------------------------------------------------------------


def test_enqueue_pose_l_evenement_de_reveil_apres_commit(db):
    from backend.search.observations import record_extraction_observation
    from backend.services import memoire_scheduler as S

    ws = _workspace(db)
    art = _artifact(db)
    _lien(db, ws, art)
    texte = "Le serveur utilise SQLite comme base de données depuis 2024."
    S._reveil_doc.clear()

    obs = record_extraction_observation(
        art,
        "attachment_ingest",
        None,
        texte,
        ok=True,
        tool_version="test/1",
    )

    assert obs is not None
    assert db.query(MemoryDocumentExtraction).one().status == "pending"
    assert S._reveil_doc.is_set()
    S._reveil_doc.clear()


def test_enfiler_observation_ne_reveille_pas_avant_le_commit(db):
    """L'enqueue transactionnel ne doit pas armer le réveil.

    `record_extraction_observation` signale APRÈS son commit : un signal émis
    ici, dans la transaction, ferait une passe à vide et survivrait à un
    rollback. L'appel direct reste donc silencieux.
    """
    from backend.services import memoire_scheduler as S

    ws = _workspace(db)
    art = _artifact(db)
    _lien(db, ws, art)
    texte = "Le serveur utilise SQLite comme base de données depuis 2024."
    obs = _observation(db, art, texte)
    S._reveil_doc.clear()

    crees = D.enfiler_observation(db, obs)
    db.commit()

    assert crees >= 1
    assert not S._reveil_doc.is_set()


# ---------------------------------------------------------------------------
# 20. Plafond de génération et détection de troncature des tool calls
# ---------------------------------------------------------------------------


def _fait_brut(texte: str, chunk_id: str) -> dict:
    return {
        "content": "Le serveur utilise SQLite comme base de données.",
        "category": "connaissances_de_travail",
        "affirmation_status": "observation",
        "quote": texte,
        "chunk_id": chunk_id,
    }


def test_plafond_640_transmis_au_runtime_et_au_manager():
    """La borne de génération vaut 640 et atteint les DEUX chemins d'appel."""
    assert E.MAX_TOKENS_LLM == 640
    messages = ({"role": "user", "content": "x"},)

    runtime = FauxRuntime([])
    E._appeler_modele(
        messages=messages,
        runtime=runtime,
        manager=None,
        timeout_s=E.TIMEOUT_LLM_S,
    )
    assert runtime.requetes[0].max_tokens == 640

    manager = FauxManager(_payload_faits([]))
    E._appeler_modele(
        messages=messages,
        runtime=None,
        manager=manager,
        timeout_s=E.TIMEOUT_LLM_S,
    )
    assert manager.modele.appels[0]["max_tokens"] == 640


def test_tool_calls_au_plafond_replie_meme_si_json_valide(db):
    """`finish_reason="tool_calls"` + compteur au plafond = sortie coupée."""
    ws = _workspace(db)
    art = _artifact(db)
    _lien(db, ws, art)
    texte = "Le serveur utilise SQLite comme base de données depuis 2024."
    _chunks(db, art, texte)
    chunk = _premier_chunk(db, art)

    payload = _payload_faits([_fait_brut(texte, chunk.id)])
    payload["choices"][0]["finish_reason"] = "tool_calls"
    payload["usage"] = {"completion_tokens": E.MAX_TOKENS_LLM}
    runtime = FauxRuntime(payload=payload)

    faits = _extraire_avec(db, runtime, texte=texte)

    assert len(runtime.requetes) == 1
    assert len(faits) == 1
    assert faits[0].method == "deterministe"


def test_tool_calls_sous_le_plafond_accepte_le_json(db):
    ws = _workspace(db)
    art = _artifact(db)
    _lien(db, ws, art)
    texte = "Le serveur utilise SQLite comme base de données depuis 2024."
    _chunks(db, art, texte)
    chunk = _premier_chunk(db, art)

    payload = _payload_faits([_fait_brut(texte, chunk.id)])
    payload["choices"][0]["finish_reason"] = "tool_calls"
    payload["usage"] = {"completion_tokens": E.MAX_TOKENS_LLM - 111}
    runtime = FauxRuntime(payload=payload)

    faits = _extraire_avec(db, runtime, texte=texte)

    assert len(runtime.requetes) == 1
    assert len(faits) == 1
    assert faits[0].method == "llm"


def test_usage_absent_accepte_le_json_sans_faux_positif(db):
    """Sans `usage`, seul `finish_reason` tranche : pas de repli inventé."""
    ws = _workspace(db)
    art = _artifact(db)
    _lien(db, ws, art)
    texte = "Le serveur utilise SQLite comme base de données depuis 2024."
    _chunks(db, art, texte)
    chunk = _premier_chunk(db, art)

    payload = _payload_faits([_fait_brut(texte, chunk.id)])
    payload["choices"][0]["finish_reason"] = "tool_calls"
    assert "usage" not in payload
    runtime = FauxRuntime(payload=payload)

    faits = _extraire_avec(db, runtime, texte=texte)

    assert len(runtime.requetes) == 1
    assert len(faits) == 1
    assert faits[0].method == "llm"


def test_troncature_detectee_sur_usage_normalise():
    """La forme normalisée (`reponse.usage`) porte le même signal."""
    au_plafond = SimpleNamespace(
        finish_reason="tool_calls",
        usage=SimpleNamespace(completion_tokens=E.MAX_TOKENS_LLM),
        raw={},
    )
    assert E._reponse_tronquee(au_plafond) is True

    sous_le_plafond = SimpleNamespace(
        finish_reason="tool_calls",
        usage=SimpleNamespace(completion_tokens=E.MAX_TOKENS_LLM - 1),
        raw={},
    )
    assert E._reponse_tronquee(sous_le_plafond) is False

    sans_usage = SimpleNamespace(finish_reason="tool_calls", raw={})
    assert E._reponse_tronquee(sans_usage) is False
