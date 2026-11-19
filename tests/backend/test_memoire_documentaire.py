"""Extraction de faits documentaires (Phase 8, fondation).

Ces tests verrouillent les propriétés qui ne se voient pas à l'usage :

- le ledger est la seule porte d'exécution, et il est idempotent ;
- un enqueue raté ne perd jamais l'observation — le backfill le rattrape ;
- une citation non vérifiée n'est jamais un fait ;
- une preuve de chunk est stable à contenu identique, et périmée sinon ;
- une correction utilisateur n'est jamais écrasée par une réextraction.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

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
from backend.search import cards as C
from backend.search import chunks as K
from backend.services import activity
from backend.services import memoire as M
from backend.services import memoire_documentaire as D


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture(autouse=True)
def _etat_propre():
    activity.marquer_tour_actif(False)
    D.reinitialiser_compteurs_enqueue()
    yield
    activity.marquer_tour_actif(False)
    with activity._OWNER_LOCK:
        activity._OWNER = None
    try:
        activity._RESERVATION_LOCK.release()
    except RuntimeError:
        pass


@pytest.fixture()
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'doc.db'}")
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    monkeypatch.setattr("backend.db.database.SessionLocal", TestSession)
    # Pas de modèle d'embedding dans ces tests : le FTS et les ids de chunks
    # n'en dépendent pas, et charger ColBERT rendrait la suite lente.
    monkeypatch.setattr(K, "_indexer_fragment", lambda *_a, **_k: True)
    s = TestSession()
    try:
        K.ensure_chunk_tables(s)
        from backend.search import fts

        fts.ensure_fts(s)
        yield s
    finally:
        s.close()


def _workspace(db, workspace_id: str = "ws_doc") -> str:
    db.add(Workspace(id=workspace_id, name="Espace documentaire"))
    db.commit()
    return workspace_id


def _artifact(
    db,
    artifact_id: str = "art_doc",
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
    observation_id: str = "obs_doc_1",
    status: str = "completed",
) -> Observation:
    obs = Observation(
        id=observation_id,
        artifact_id=artifact_id,
        tool_name="attachment_ingest",
        tool_version="test/1",
        status=status,
        payload_ref=payload,
        result_json="{}",
    )
    db.add(obs)
    db.commit()
    return obs


def _chunks(db, artifact_id: str, texte: str, workspace_id: str = "ws_doc") -> int:
    art = db.query(Artifact).filter(Artifact.id == artifact_id).first()
    C.ensure_card_on_import(db, art)
    n = K.ensure_chunks_for_artifact(db, art, texte, [("workspace", workspace_id)])
    db.commit()
    return n


def _fait(
    *,
    content: str,
    entry_key: str = "doc:ws_doc:art_doc:abc",
    artifact_id: str = "art_doc",
    observation_id: str = "obs_doc_1",
    chunk_id: str = "chk_x",
    quote: str | None = None,
    workspace_id: str = "ws_doc",
) -> D.FaitDocument:
    return D.FaitDocument(
        content=content,
        quote=quote or content,
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
    )


# ---------------------------------------------------------------------------
# 8.3 — extraction déterministe
# ---------------------------------------------------------------------------


def test_extraction_retient_phrases_citees_et_borne_a_cinq(db):
    ws = _workspace(db)
    art = _artifact(db)
    _lien(db, ws, art)
    phrases = [
        "Le projet utilise SQLite comme base de données principale depuis 2024.",
        "Le serveur tourne sous Debian 12 avec 32 Go de mémoire vive.",
        "La sauvegarde automatique reste activée chaque nuit à 3 heures.",
        "Le document doit rester sous 8 Go de mémoire pour le GPU.",
        "L'équipe utilise un format de réponse court et structuré en 2026.",
        "Le fichier de configuration doit contenir 12 paramètres obligatoires.",
    ]
    texte = " ".join(phrases)
    assert _chunks(db, art, texte) >= 1

    faits = D.extraire_faits_document(
        db,
        artifact_id=art,
        workspace_id=ws,
        texte=texte,
        observation_id="obs_doc_1",
        extractor_version=D.VERSION_EXTRACTEUR,
        card_revision=1,
    )

    assert len(faits) == 5
    for fait in faits:
        assert D.MIN_FAIT_CHARS <= len(fait.content) <= D.MAX_FAIT_CHARS
        assert fait.quote in texte
        assert fait.chunk_id
        assert "obs_doc_1" in fait.evidence_ids
        assert fait.chunk_id in fait.evidence_ids
        assert fait.origin_context.startswith("document:art_doc:observation:obs_doc_1:")
        assert fait.source_artifact_ids == [art]


def test_valider_faits_structure_rejette_une_citation_hors_du_chunk(db):
    """🟡 4 — une citation doit exister DANS le fragment qu'elle désigne.

    Un extracteur structuré peut inventer un `chunk_id` ou rattacher une phrase
    au mauvais fragment. Le validateur doit refuser le lot (le repli s'applique),
    sinon la provenance ment sur l'origine de la preuve.
    """
    ws = _workspace(db)
    art = _artifact(db)
    _lien(db, ws, art)
    texte = "Le serveur utilise SQLite comme base de données depuis 2024."
    _chunks(db, art, texte)
    chunk = db.query(ArtifactChunk).filter_by(artifact_id=art).first()

    hors_chunk = _fait(
        content="Le serveur utilise PostgreSQL depuis 2025.",
        quote="Le serveur utilise PostgreSQL depuis 2025.",
        chunk_id=chunk.id,
    )
    assert (
        D._valider_faits_structure(
            [hors_chunk],
            extractor_version=D.VERSION_EXTRACTEUR,
            db=db,
            artifact_id=art,
        )
        is None
    )

    dans_chunk = _fait(
        content="Le serveur utilise SQLite depuis 2024.",
        quote="Le serveur utilise SQLite comme base de données depuis 2024.",
        chunk_id=chunk.id,
    )
    valides = D._valider_faits_structure(
        [dans_chunk],
        extractor_version=D.VERSION_EXTRACTEUR,
        db=db,
        artifact_id=art,
    )
    assert valides is not None
    assert len(valides) == 1
    assert valides[0].chunk_id == chunk.id


def test_phrase_trop_courte_longue_sans_verbe_ou_sans_citation_est_rejetee(db):
    ws = _workspace(db)
    art = _artifact(db)
    _lien(db, ws, art)
    valide = "Le projet utilise SQLite comme base de données principale depuis 2024."
    trop_courte = "SQLite est utilisé."
    sans_verbe = "Une base de données SQLite en 2024 pour le projet."
    trop_longue = (
        "Le projet utilise SQLite comme base de données principale depuis 2024 "
        "et cette phrase est volontairement rallongée pour dépasser largement la "
        "borne maximale de trois cents caractères que le contrat impose à un "
        "fait documentaire, en ajoutant encore des mots inutiles jusqu'à ce que "
        "la limite soit franchie, puis un peu plus pour être certain."
    )
    texte = " ".join([valide, trop_courte, sans_verbe, trop_longue])
    _chunks(db, art, texte)

    faits = D.extraire_faits_document(
        db,
        artifact_id=art,
        workspace_id=ws,
        texte=texte,
        observation_id="obs_doc_1",
        extractor_version=D.VERSION_EXTRACTEUR,
        card_revision=1,
    )

    assert [f.content for f in faits] == [valide]


def test_citation_absente_du_fragment_est_rejetee(db):
    ws = _workspace(db)
    art = _artifact(db)
    _lien(db, ws, art)
    # Les fragments ne contiennent PAS la phrase soumise à l'extraction.
    _chunks(db, art, "Un contenu totalement différent sans le fait invoqué.")

    faits = D.extraire_faits_document(
        db,
        artifact_id=art,
        workspace_id=ws,
        texte="Le projet utilise SQLite comme base de données principale depuis 2024.",
        observation_id="obs_doc_1",
        extractor_version=D.VERSION_EXTRACTEUR,
        card_revision=1,
    )

    assert faits == []


def test_zero_fait_sur_document_sans_texte(db):
    ws = _workspace(db)
    art = _artifact(db, state="vide")
    assert (
        D.extraire_faits_document(
            db,
            artifact_id=art,
            workspace_id=ws,
            texte="",
            observation_id=None,
        )
        == []
    )


# ---------------------------------------------------------------------------
# 8.5 — stabilité des preuves
# ---------------------------------------------------------------------------


def test_chunk_id_stable_a_revision_constante_et_change_apres_reingestion(db):
    ws = _workspace(db)
    art = _artifact(db)
    _lien(db, ws, art)

    def ids_par_ordinal():
        return {
            c.ordinal: c.id
            for c in db.query(ArtifactChunk).filter_by(artifact_id=art).all()
        }

    texte = "Le protocole de sauvegarde exige deux copies chiffrées. " * 8

    # 1. Ingestion initiale (fiche créée en révision 1).
    _chunks(db, art, texte)
    premier = ids_par_ordinal()
    assert premier

    # 2. Réingestion à révision CONSTANTE (même fiche) : les ids sont stables.
    _chunks(db, art, texte)
    assert ids_par_ordinal() == premier, (
        "à révision de fiche constante, une réingestion identique conserve les ids"
    )

    # 3. VRAIE réingestion : une nouvelle observation crée une nouvelle révision
    # de fiche (`revise_card_after_observation`), comme en production.
    from backend.db.models import Artifact as _Art
    from backend.search.cards import revise_card_after_observation

    obs = _observation(db, art, texte, observation_id="obs_reingestion")
    art_obj = db.query(_Art).filter(_Art.id == art).first()
    revise_card_after_observation(db, art_obj, obs, excerpt=texte[:200])
    _chunks(db, art, texte)

    apres_revision = ids_par_ordinal()
    # CONTRAT TRANCHÉ (relevé Phase 8, 🟠 3) : « stable à révision constante ».
    # Une nouvelle révision change les ids même à texte identique, pour que la
    # preuve reste attachée à la révision qui l'a produite. Ce n'est donc PAS
    # une stabilité à texte identique toutes révisions confondues.
    assert set(premier.values()).isdisjoint(set(apres_revision.values())), (
        "une nouvelle révision de fiche doit renouveler les ids de fragments"
    )

    # 4. Un texte différent à révision constante change aussi les ids.
    _chunks(db, art, "Le protocole de restauration exige trois copies locales. " * 8)
    apres_texte = ids_par_ordinal()
    assert set(apres_revision.values()).isdisjoint(set(apres_texte.values()))


def test_provenance_conforme_et_recopiee_en_version_n_plus_un(db):
    ws = _workspace(db)
    art = _artifact(db)
    _lien(db, ws, art)
    fait = _fait(content="Le serveur utilise SQLite depuis 2024.")

    D.appliquer_faits_documentaires(
        db,
        workspace_id=ws,
        artifact_id=art,
        faits=[fait],
        observation_id="obs_doc_1",
        extractor_version=D.VERSION_EXTRACTEUR,
    )
    entry = db.query(MemoryEntry).filter(MemoryEntry.is_current == 1).one()
    assert entry.source_artifact_ids == [art]
    assert entry.source_observation_id == "obs_doc_1"
    assert entry.extractor_version == D.VERSION_EXTRACTEUR
    assert entry.origin_context == fait.origin_context
    assert "obs_doc_1" in entry.evidence_ids
    assert "chk_x" in entry.evidence_ids
    assert {
        "kind": "document",
        "method": "deterministe",
        "version": D.VERSION_EXTRACTEUR,
    } in entry.promotion_reasons

    fait2 = _fait(
        content="Le serveur utilise SQLite depuis 2024.",
        observation_id="obs_doc_2",
        chunk_id="chk_x",
    )
    D.appliquer_faits_documentaires(
        db,
        workspace_id=ws,
        artifact_id=art,
        faits=[fait2],
        observation_id="obs_doc_2",
        extractor_version=D.VERSION_EXTRACTEUR,
    )
    courante = db.query(MemoryEntry).filter(MemoryEntry.is_current == 1).one()
    assert courante.version == 2
    assert courante.source_observation_id == "obs_doc_2"
    assert courante.extractor_version == D.VERSION_EXTRACTEUR


# ---------------------------------------------------------------------------
# 8.2 / 8.2b — enqueue, skip, backfill, relink
# ---------------------------------------------------------------------------


def test_echec_enqueue_conserve_observation_puis_backfill_recupere(db):
    from backend.search.observations import record_extraction_observation

    ws = _workspace(db)
    art = _artifact(db, state="extrait")
    _lien(db, ws, art)
    texte = "Le projet utilise SQLite comme base de données principale depuis 2024."

    def boom(_db, _obs):
        raise RuntimeError("panne simulée d'enqueue")

    obs = record_extraction_observation(
        art,
        "attachment_ingest",
        None,
        texte,
        ok=True,
        tool_version="test/1",
        enqueue_documentaire=boom,
    )

    assert obs is not None
    assert db.query(Observation).filter(Observation.id == obs.id).count() == 1
    assert db.query(MemoryDocumentExtraction).count() == 0
    assert D.compteurs_enqueue()["echecs"] == 1

    compteurs = D.backfiller_ledger_documentaire(db)
    assert compteurs["crees"] == 1
    ligne = db.query(MemoryDocumentExtraction).one()
    assert ligne.status == "pending"
    assert ligne.observation_id == obs.id

    # Idempotent : un second backfill ne duplique rien.
    assert D.backfiller_ledger_documentaire(db)["crees"] == 0


def test_backfill_apres_skip_anterieur_et_echec_enqueue_nouvelle_observation(db):
    from backend.search.observations import record_extraction_observation

    ws = _workspace(db)
    art = _artifact(db, state="extrait")
    _lien(db, ws, art)

    # Ligne `skipped` d'une observation ANTÉRIEURE, comme après un premier
    # passage sans texte exploitable.
    obs_ancienne = _observation(db, art, "ancien texte", observation_id="obs_ancienne")
    D._inserer_ligne(
        db,
        workspace_id=ws,
        artifact_id=art,
        observation_id=obs_ancienne.id,
        extractor_version=D.VERSION_EXTRACTEUR,
        status="skipped",
        card_revision=0,
        last_error="ancien échec",
    )
    db.commit()

    # Une NOUVELLE observation arrive avec du texte, mais le hook d'enqueue
    # échoue : aucune ligne n'est écrite pour elle.
    def boom(_db, _obs):
        raise RuntimeError("panne simulée d'enqueue")

    obs = record_extraction_observation(
        art,
        "attachment_ingest",
        None,
        "Le serveur tourne sous Debian 12 depuis 2024 et utilise SQLite.",
        ok=True,
        tool_version="test/1",
        enqueue_documentaire=boom,
    )
    assert obs is not None
    assert obs.id != obs_ancienne.id
    assert db.query(MemoryDocumentExtraction).filter_by(status="skipped").count() == 1

    # Le backfill doit créer une ligne `pending` pour la NOUVELLE observation :
    # le skipped antérieur ne la masque pas (🟠 1).
    compteurs = D.backfiller_ledger_documentaire(db)
    assert compteurs["crees"] == 1
    lignes = db.query(MemoryDocumentExtraction).all()
    assert len(lignes) == 2
    pending = [l for l in lignes if l.status == "pending"]
    assert len(pending) == 1
    assert pending[0].observation_id == obs.id


def test_enqueue_skip_quand_a_texte_est_faux_et_ne_retente_pas(db):
    ws = _workspace(db)
    art = _artifact(db, state="binaire_refuse")
    _lien(db, ws, art)
    obs = _observation(db, art, "")

    assert D.enfiler_observation(db, obs) == 1
    ligne = db.query(MemoryDocumentExtraction).one()
    assert ligne.status == "skipped"
    assert "aucun texte" in (ligne.last_error or "")

    # Le backfill ne recrée pas une ligne pour un document sans texte.
    assert D.backfiller_ledger_documentaire(db)["crees"] == 0
    assert db.query(MemoryDocumentExtraction).count() == 1


def test_relink_sans_texte_ne_cree_aucune_ligne(db):
    """🟡 7 — « pas de ledger sans texte » est LITTÉRAL pour le relink.

    Un artefact encore illisible ne produit ni `pending` ni `skipped` : une
    ligne terminale masquerait l'enqueue ultérieur quand le texte arrive. Le
    backfill créera la ligne `pending` à ce moment-là.
    """
    ws = _workspace(db)
    art = _artifact(db, state="binaire_refuse")
    _lien(db, ws, art)

    assert D.enfiler_apres_relink(db, ws, art) == 0
    assert db.query(MemoryDocumentExtraction).count() == 0

    # Le backfill non plus ne crée rien tant qu'il n'y a pas de texte.
    assert D.backfiller_ledger_documentaire(db)["crees"] == 0
    assert db.query(MemoryDocumentExtraction).count() == 0


def test_observation_reussie_enfile_par_defaut(db):
    from backend.search.observations import record_extraction_observation

    ws = _workspace(db)
    art = _artifact(db, state="extrait")
    _lien(db, ws, art)
    texte = "Le projet utilise SQLite comme base de données principale depuis 2024."

    obs = record_extraction_observation(
        art,
        "attachment_ingest",
        None,
        texte,
        ok=True,
        tool_version="test/1",
    )

    assert obs is not None
    ligne = db.query(MemoryDocumentExtraction).one()
    assert ligne.status == "pending"
    assert ligne.observation_id == obs.id


def test_execution_de_bout_en_bout_ecrit_un_fait_cite(db):
    from backend.search.observations import record_extraction_observation

    ws = _workspace(db)
    art = _artifact(db, state="extrait")
    _lien(db, ws, art)
    texte = "Le serveur utilise SQLite comme base de données depuis 2024. " * 5

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

    compteurs = D.executer_lot_documentaire()

    assert compteurs["done"] == 1
    ligne = db.query(MemoryDocumentExtraction).one()
    assert ligne.status == "done"
    assert ligne.attempts == 1
    assert ligne.observation_id == obs.id
    entry = db.query(MemoryEntry).filter(MemoryEntry.is_current == 1).one()
    assert (
        entry.content == "Le serveur utilise SQLite comme base de données depuis 2024."
    )
    assert obs.id in entry.evidence_ids
    assert ligne.chunk_id in entry.evidence_ids
    assert entry.source_observation_id == obs.id
    assert D.VERSION_EXTRACTEUR in {
        raison.get("version")
        for raison in entry.promotion_reasons
        if isinstance(raison, dict)
    }


def test_backfill_traite_une_piece_jointe_de_conversation_rattachee(db):
    from backend.db.models import Conversation, ArtifactLink

    ws = _workspace(db)
    art = _artifact(db, state="extrait")
    db.add(Conversation(id="conv_espace", title="Discussion", workspace_id=ws))
    db.add(
        ArtifactLink(
            id="lnk_conv",
            artifact_id=art,
            scope_kind="conversation",
            scope_id="conv_espace",
            role="attachment",
        )
    )
    db.commit()
    _observation(db, art, "Le serveur tourne sous Debian 12 depuis 2024.")

    assert D.backfiller_ledger_documentaire(db)["crees"] == 1
    ligne = db.query(MemoryDocumentExtraction).one()
    assert ligne.workspace_id == ws
    assert ligne.status == "pending"


def test_backfill_traite_un_espace_sans_conversation_ni_tours(db):
    ws = _workspace(db)
    art = _artifact(db, state="extrait")
    _lien(db, ws, art)
    _observation(db, art, "Le serveur tourne sous Debian 12 depuis 2024.")

    compteurs = D.backfiller_ledger_documentaire(db)
    assert compteurs["crees"] == 1
    assert db.query(Workspace).one().auto_memory_turn_count == 0
    assert db.query(MemoryDocumentExtraction).one().status == "pending"
    assert D.backfiller_ledger_documentaire(db)["crees"] == 0


def test_relink_apres_coup_enfile_le_document(db):
    ws = _workspace(db)
    art = _artifact(db, state="extrait")
    _observation(db, art, "Le serveur tourne sous Debian 12 depuis 2024.")
    _lien(db, ws, art)

    assert D.enfiler_apres_relink(db, ws, art) == 1
    assert db.query(MemoryDocumentExtraction).one().status == "pending"
    assert D.enfiler_apres_relink(db, ws, art) == 0


# ---------------------------------------------------------------------------
# 8.6 — claim, bail, budget, resource busy
# ---------------------------------------------------------------------------


def _ledger(
    db,
    *,
    artifact_id: str = "art_doc",
    workspace_id: str = "ws_doc",
    observation_id: str | None = "obs_doc_1",
    status: str = "pending",
    attempts: int = 0,
    claimed_at: datetime | None = None,
) -> MemoryDocumentExtraction:
    ligne = MemoryDocumentExtraction(
        id=f"mdx_{status}_{artifact_id}_{attempts}",
        workspace_id=workspace_id,
        artifact_id=artifact_id,
        observation_id=observation_id,
        extractor_version=D.VERSION_EXTRACTEUR,
        status=status,
        attempts=attempts,
        claimed_at=claimed_at,
    )
    db.add(ligne)
    db.commit()
    return ligne


def test_claim_concurrent_un_seul_gagne(db):
    ws = _workspace(db)
    art = _artifact(db)
    _lien(db, ws, art)
    _observation(db, art, "Le serveur tourne sous Debian 12 depuis 2024.")
    _ledger(db, artifact_id=art, workspace_id=ws)

    premier = D.claim_prochain_document(db)
    second = D.claim_prochain_document(db)

    assert premier is not None
    assert second is None
    ligne = db.query(MemoryDocumentExtraction).one()
    assert ligne.status == "running"
    assert ligne.claim_token == premier["claim_token"]


def test_claim_concurrent_deux_sessions_une_seule_gagne(db):
    """🟡 5 — le CAS doit tenir avec deux sessions concurrentes, pas seulement
    deux appels séquentiels sur la même session."""
    import threading

    from sqlalchemy.orm import sessionmaker

    ws = _workspace(db)
    art = _artifact(db)
    _lien(db, ws, art)
    _observation(db, art, "Le serveur tourne sous Debian 12 depuis 2024.")
    _ledger(db, artifact_id=art, workspace_id=ws)
    db.commit()

    Session = sessionmaker(
        bind=db.get_bind(),
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )
    barrier = threading.Barrier(2)
    resultats: list = []
    erreurs: list = []

    def worker() -> None:
        session = Session()
        try:
            barrier.wait(timeout=5)
            resultats.append(D.claim_prochain_document(session))
        except Exception as exc:  # noqa: BLE001 — remonté à l'assertion
            erreurs.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not erreurs, erreurs
    gagnants = [r for r in resultats if r is not None]
    assert len(gagnants) == 1, "le CAS doit laisser exactement un gagnant"
    db.expire_all()
    ligne = db.query(MemoryDocumentExtraction).one()
    assert ligne.status == "running"
    assert ligne.claim_token == gagnants[0]["claim_token"]


def test_running_expire_repris_incremente_attempts(db):
    ws = _workspace(db)
    art = _artifact(db)
    _lien(db, ws, art)
    _observation(db, art, "Le serveur tourne sous Debian 12 depuis 2024.")
    _ledger(
        db,
        artifact_id=art,
        workspace_id=ws,
        status="running",
        attempts=1,
        claimed_at=_now() - timedelta(seconds=D.TTL_BAIL_S + 30),
    )

    claim = D.claim_prochain_document(db)

    assert claim is not None
    ligne = db.query(MemoryDocumentExtraction).one()
    assert ligne.status == "running"
    assert ligne.claim_token == claim["claim_token"]
    # Politique de reprise : une expiration de bail compte une tentative. Sans
    # cela, un worker stallé en boucle contournerait le plafond 3.
    assert ligne.attempts == 2
    assert claim["attempts"] == 2


def test_running_expire_au_plafond_devient_failed(db):
    ws = _workspace(db)
    art = _artifact(db)
    _lien(db, ws, art)
    _observation(db, art, "Le serveur tourne sous Debian 12 depuis 2024.")
    _ledger(
        db,
        artifact_id=art,
        workspace_id=ws,
        status="running",
        attempts=D.PLAFOND_EXECUTIONS - 1,
        claimed_at=_now() - timedelta(seconds=D.TTL_BAIL_S + 30),
    )

    claim = D.claim_prochain_document(db)

    assert claim is None, "le plafond de reprises ne doit pas être contourné"
    ligne = db.query(MemoryDocumentExtraction).one()
    assert ligne.status == "failed"
    assert ligne.attempts == D.PLAFOND_EXECUTIONS
    assert "plafond" in (ligne.last_error or "")


def test_resource_busy_differe_sans_attempts(db, monkeypatch):
    ws = _workspace(db)
    art = _artifact(db)
    _lien(db, ws, art)
    obs = _observation(db, art, "Le serveur tourne sous Debian 12 depuis 2024.")
    _ledger(db, artifact_id=art, workspace_id=ws, observation_id=obs.id)

    def occupe(*_args, **_kwargs):
        raise ResourceBusy()

    monkeypatch.setattr(D, "extraire_faits_document", occupe)
    compteurs = D.executer_lot_documentaire()

    ligne = db.query(MemoryDocumentExtraction).one()
    assert compteurs["deferred"] == 1
    assert ligne.status == "deferred"
    assert ligne.attempts == 0
    assert ligne.deferrals == 1
    assert ligne.next_attempt_at is not None


def test_tour_actif_differe_sans_attempts(db):
    ws = _workspace(db)
    art = _artifact(db)
    _lien(db, ws, art)
    obs = _observation(db, art, "Le serveur tourne sous Debian 12 depuis 2024.")
    _ledger(db, artifact_id=art, workspace_id=ws, observation_id=obs.id)

    activity.marquer_tour_actif(True)
    D.executer_lot_documentaire()

    ligne = db.query(MemoryDocumentExtraction).one()
    assert ligne.status == "deferred"
    assert ligne.attempts == 0
    assert ligne.deferrals == 1


def test_budget_respecte_deux_documents(db, monkeypatch):
    ws = _workspace(db)
    for i in range(3):
        art = _artifact(db, f"art_{i}")
        _lien(db, ws, art)
        obs = _observation(
            db,
            art,
            "Le serveur tourne sous Debian 12 depuis 2024.",
            observation_id=f"obs_{i}",
        )
        _ledger(db, artifact_id=art, workspace_id=ws, observation_id=obs.id)

    monkeypatch.setattr(D, "extraire_faits_document", lambda *_a, **_k: [])
    compteurs = D.executer_lot_documentaire(budget_documents=2)

    assert compteurs["traites"] == 2
    assert compteurs["done"] == 2
    assert db.query(MemoryDocumentExtraction).filter_by(status="pending").count() == 1


def test_journalise_extraction_sans_changer_le_comportement(db, monkeypatch, caplog):
    ws = _workspace(db)
    art = _artifact(db)
    _lien(db, ws, art)
    obs = _observation(db, art, "Le serveur tourne sous Debian 12 depuis 2024.")
    _ledger(db, artifact_id=art, workspace_id=ws, observation_id=obs.id)

    fait = _fait(content="Le serveur tourne sous Debian 12 depuis 2024.")
    monkeypatch.setattr(D, "extraire_faits_document", lambda *_a, **_k: [fait])
    instants = iter((10.0, 10.125))
    monkeypatch.setattr(D.time, "perf_counter", lambda: next(instants))

    with caplog.at_level(logging.INFO, logger=D.__name__):
        compteurs = D.executer_lot_documentaire()

    assert compteurs["done"] == 1
    lignes = [
        enregistrement.getMessage()
        for enregistrement in caplog.records
        if enregistrement.name == D.__name__
        and "memoire_documentaire extraction" in enregistrement.getMessage()
    ]
    assert len(lignes) == 1
    ligne = lignes[0]
    assert f"artifact_id={art}" in ligne
    assert f"document_id={art}" in ligne
    assert "duration_ms=125.0" in ligne
    assert "method=deterministe" in ligne
    assert "facts=1" in ligne
    assert "fallback=non" in ligne


def test_bail_perdu_aucune_ecriture(db, monkeypatch):
    ws = _workspace(db)
    art = _artifact(db)
    _lien(db, ws, art)
    obs = _observation(db, art, "Le serveur tourne sous Debian 12 depuis 2024.")
    ligne = _ledger(db, artifact_id=art, workspace_id=ws, observation_id=obs.id)

    fait = _fait(content="Le serveur utilise SQLite depuis 2024.")

    def vole_et_extrait(*_args, **kwargs):
        # Un autre worker reprend le bail pendant l'extraction.
        db.query(MemoryDocumentExtraction).filter_by(id=ligne.id).update(
            {"claim_token": "token_du_voleur", "claimed_at": _now()}
        )
        db.commit()
        return [fait]

    monkeypatch.setattr(D, "extraire_faits_document", vole_et_extrait)
    compteurs = D.executer_lot_documentaire()

    assert compteurs["bail_perdu"] == 1
    assert db.query(MemoryEntry).count() == 0
    db.expire_all()
    ligne = db.query(MemoryDocumentExtraction).one()
    assert ligne.status == "running"
    assert ligne.claim_token == "token_du_voleur"


# ---------------------------------------------------------------------------
# 8.7 — remplacement
# ---------------------------------------------------------------------------


def test_version_n_plus_un_et_reformulation_non_dupliquee(db):
    ws = _workspace(db)
    art = _artifact(db)
    _lien(db, ws, art)
    D.appliquer_faits_documentaires(
        db,
        workspace_id=ws,
        artifact_id=art,
        faits=[_fait(content="Le serveur utilise SQLite comme base depuis 2024.")],
        observation_id="obs_doc_1",
        extractor_version=D.VERSION_EXTRACTEUR,
    )

    # Clé différente (contenu reformulé) mais Jaccard ≥ 0,8.
    reformule = _fait(
        content="Le serveur utilise SQLite comme base depuis 2024 aussi.",
        entry_key="doc:ws_doc:art_doc:reformule",
        observation_id="obs_doc_2",
        chunk_id="chk_y",
    )
    D.appliquer_faits_documentaires(
        db,
        workspace_id=ws,
        artifact_id=art,
        faits=[reformule],
        observation_id="obs_doc_2",
        extractor_version=D.VERSION_EXTRACTEUR,
    )

    courantes = db.query(MemoryEntry).filter(MemoryEntry.is_current == 1).all()
    assert len(courantes) == 1, "une reformulation ne crée pas une seconde fiche"
    assert courantes[0].version == 2
    assert "chk_y" in courantes[0].evidence_ids


def test_fait_non_reproduit_devient_stale_motive(db):
    ws = _workspace(db)
    art = _artifact(db)
    _lien(db, ws, art)
    D.appliquer_faits_documentaires(
        db,
        workspace_id=ws,
        artifact_id=art,
        faits=[_fait(content="Le serveur utilise SQLite comme base depuis 2024.")],
        observation_id="obs_doc_1",
        extractor_version=D.VERSION_EXTRACTEUR,
    )

    D.appliquer_faits_documentaires(
        db,
        workspace_id=ws,
        artifact_id=art,
        faits=[
            _fait(
                content="La sauvegarde exige trois copies chiffrées chaque nuit.",
                entry_key="doc:ws_doc:art_doc:autre",
                observation_id="obs_doc_2",
                chunk_id="chk_z",
            )
        ],
        observation_id="obs_doc_2",
        extractor_version=D.VERSION_EXTRACTEUR,
    )

    ancienne = (
        db.query(MemoryEntry)
        .filter(
            MemoryEntry.entry_key == "doc:ws_doc:art_doc:abc",
            MemoryEntry.is_current == 0,
        )
        .one()
    )
    assert ancienne.version == 1
    courante = (
        db.query(MemoryEntry)
        .filter(
            MemoryEntry.entry_key == "doc:ws_doc:art_doc:abc",
            MemoryEntry.is_current == 1,
        )
        .one()
    )
    assert courante.status == "stale"
    assert courante.stale_reason == D.STALE_REASON_PREUVE_INVALIDEE


def test_changement_de_texte_invalide_l_ancienne_preuve(db):
    ws = _workspace(db)
    art = _artifact(db)
    _lien(db, ws, art)
    texte_1 = "Le serveur utilise SQLite comme base de données depuis 2024."
    _chunks(db, art, texte_1)
    faits_1 = D.extraire_faits_document(
        db,
        artifact_id=art,
        workspace_id=ws,
        texte=texte_1,
        observation_id="obs_1",
        extractor_version=D.VERSION_EXTRACTEUR,
        card_revision=1,
    )
    assert len(faits_1) == 1
    D.appliquer_faits_documentaires(
        db,
        workspace_id=ws,
        artifact_id=art,
        faits=faits_1,
        observation_id="obs_1",
        extractor_version=D.VERSION_EXTRACTEUR,
    )
    ancienne_preuve = faits_1[0].chunk_id
    assert ancienne_preuve in db.query(MemoryEntry).one().evidence_ids

    texte_2 = "La sauvegarde doit conserver 3 copies locales chaque nuit."
    _chunks(db, art, texte_2)
    faits_2 = D.extraire_faits_document(
        db,
        artifact_id=art,
        workspace_id=ws,
        texte=texte_2,
        observation_id="obs_2",
        extractor_version=D.VERSION_EXTRACTEUR,
        card_revision=1,
    )
    assert len(faits_2) == 1
    D.appliquer_faits_documentaires(
        db,
        workspace_id=ws,
        artifact_id=art,
        faits=faits_2,
        observation_id="obs_2",
        extractor_version=D.VERSION_EXTRACTEUR,
    )

    ancienne = (
        db.query(MemoryEntry)
        .filter(
            MemoryEntry.entry_key == faits_1[0].entry_key, MemoryEntry.is_current == 1
        )
        .one()
    )
    assert ancienne.status == "stale"
    assert ancienne.stale_reason == D.STALE_REASON_PREUVE_INVALIDEE
    assert ancienne_preuve not in {
        c.id for c in db.query(ArtifactChunk).filter_by(artifact_id=art).all()
    }


def test_fenetre_suppression_chunks_perime_les_preuves_avant_application(db):
    """🟠 2 — la péremption appartient à la transaction qui supprime les chunks.

    Sans cela, entre l'ingestion (qui supprime les anciens `chk_`) et
    `appliquer_faits_documentaires` (qui marque `stale`), une fiche COURANTE
    cite un `chk_` inexistant. Le test vérifie la fenêtre, pas seulement l'état
    final.
    """
    ws = _workspace(db)
    art = _artifact(db)
    _lien(db, ws, art)
    texte_1 = "Le serveur utilise SQLite comme base de données depuis 2024."
    _chunks(db, art, texte_1)
    faits_1 = D.extraire_faits_document(
        db,
        artifact_id=art,
        workspace_id=ws,
        texte=texte_1,
        observation_id="obs_fenetre_1",
        extractor_version=D.VERSION_EXTRACTEUR,
        card_revision=1,
    )
    assert len(faits_1) == 1
    D.appliquer_faits_documentaires(
        db,
        workspace_id=ws,
        artifact_id=art,
        faits=faits_1,
        observation_id="obs_fenetre_1",
        extractor_version=D.VERSION_EXTRACTEUR,
    )
    ancienne_preuve = faits_1[0].chunk_id
    assert ancienne_preuve in db.query(MemoryEntry).one().evidence_ids

    # Réingestion : `_chunks` supprime les anciens fragments. La fiche qui les
    # citait doit être périmée DANS cette transaction, avant toute application
    # de nouveaux faits.
    texte_2 = "La sauvegarde doit conserver 3 copies locales chaque nuit."
    _chunks(db, art, texte_2)

    ids_presents = {
        c.id for c in db.query(ArtifactChunk).filter_by(artifact_id=art).all()
    }
    ancienne = (
        db.query(MemoryEntry)
        .filter(
            MemoryEntry.entry_key == faits_1[0].entry_key, MemoryEntry.is_current == 1
        )
        .one()
    )
    assert ancienne.status == "stale"
    assert ancienne.stale_reason == D.STALE_REASON_PREUVE_INVALIDEE
    assert not any(
        str(e).startswith("chk_") and str(e) not in ids_presents
        for e in (ancienne.evidence_ids or [])
    ), "aucune preuve de fiche courante ne doit citer un chk_ supprimé"
    for row in db.query(MemoryEntry).filter(MemoryEntry.is_current == 1).all():
        assert not any(
            str(e).startswith("chk_") and str(e) not in ids_presents
            for e in (row.evidence_ids or [])
        )


def test_correction_utilisateur_preservee_et_texte_rattache_en_candidat(db):
    ws = _workspace(db)
    art = _artifact(db)
    _lien(db, ws, art)
    fait = _fait(content="Le serveur utilise SQLite comme base depuis 2024.")
    db.add(
        MemoryEntry(
            id="mem_user",
            entry_key=fait.entry_key,
            workspace_id=ws,
            version=1,
            is_current=1,
            content="Correction utilisateur : la base est PostgreSQL depuis 2025.",
            status="accepted",
            source_artifact_ids=[art],
            origin_context=fait.origin_context,
            promotion_reasons=[],
            evidence_ids=fait.evidence_ids,
            decision={
                "actor": "user",
                "action": "correct",
                "at": _now().isoformat(),
            },
        )
    )
    db.commit()

    D.appliquer_faits_documentaires(
        db,
        workspace_id=ws,
        artifact_id=art,
        faits=[fait],
        observation_id="obs_doc_1",
        extractor_version=D.VERSION_EXTRACTEUR,
    )

    # La version utilisateur reste courante et intacte.
    user = (
        db.query(MemoryEntry)
        .filter(MemoryEntry.entry_key == fait.entry_key, MemoryEntry.is_current == 1)
        .one()
    )
    assert user.id == "mem_user"
    assert "PostgreSQL" in user.content
    assert user.version == 1

    # Le texte ré-extrait existe, mais seulement comme candidat séparé.
    candidat = (
        db.query(MemoryEntry)
        .filter(
            MemoryEntry.workspace_id == ws,
            MemoryEntry.status == "candidate",
            MemoryEntry.is_current == 1,
        )
        .one()
    )
    assert candidat.entry_key != fait.entry_key
    assert candidat.decision["action"] == "document_candidate"


def test_backfill_rearme_done_si_version_extracteur_differente(db):
    """🟠 A — une nouvelle version reprend la MÊME observation `done`."""
    ws = _workspace(db)
    art = _artifact(db, state="extrait")
    _lien(db, ws, art)
    obs = _observation(
        db,
        art,
        "Le serveur utilise SQLite comme base de données depuis 2024.",
    )

    # Ligne `done` d'une version ANTÉRIEURE pour la même observation.
    D._inserer_ligne(
        db,
        workspace_id=ws,
        artifact_id=art,
        observation_id=obs.id,
        extractor_version="doc-faits-deterministe/0",
        status="done",
        card_revision=0,
    )
    db.commit()

    compteurs = D.backfiller_ledger_documentaire(
        db, extractor_version=D.VERSION_EXTRACTEUR
    )
    assert compteurs["crees"] == 0
    db.expire_all()
    lignes = db.query(MemoryDocumentExtraction).all()
    assert len(lignes) == 1
    ligne = lignes[0]
    assert ligne.status == "pending"
    assert ligne.extractor_version == D.VERSION_EXTRACTEUR
    assert ligne.attempts == 0
    assert ligne.deferrals == 0
    assert ligne.claim_token is None

    # Même version : `done` reste terminal, aucune nouvelle ligne.
    ligne.status = "done"
    db.commit()
    db.expire_all()
    compteurs = D.backfiller_ledger_documentaire(
        db, extractor_version=D.VERSION_EXTRACTEUR
    )
    assert compteurs["crees"] == 0
    assert db.query(MemoryDocumentExtraction).count() == 1
    ligne = db.query(MemoryDocumentExtraction).one()
    assert ligne.status == "done"
    assert ligne.extractor_version == D.VERSION_EXTRACTEUR


def test_invalidation_multi_artefacts_conserve_fiche_avec_preuve_vivante(db):
    """🟠 B — une fiche partagée n'est périmée que si TOUTES ses preuves meurent."""
    ws = _workspace(db)
    art_a = _artifact(db, artifact_id="art_a", state="extrait")
    art_b = _artifact(db, artifact_id="art_b", state="extrait")
    _lien(db, ws, art_a)
    _lien(db, ws, art_b)
    _chunks(db, art_a, "Le serveur utilise SQLite comme base de données depuis 2024.")
    _chunks(db, art_b, "La sauvegarde doit conserver 3 copies locales chaque nuit.")
    chunk_a = db.query(ArtifactChunk).filter_by(artifact_id=art_a).one().id
    chunk_b = db.query(ArtifactChunk).filter_by(artifact_id=art_b).one().id

    db.add(
        MemoryEntry(
            id="mem_multi",
            entry_key="key_multi",
            workspace_id=ws,
            version=1,
            is_current=1,
            content="Fiche partagée A et B",
            status="accepted",
            source_artifact_ids=[art_a, art_b],
            origin_context=(
                f"document:{art_a}:observation:obs_a:chunk:{chunk_a}; "
                f"document:{art_b}:observation:obs_b:chunk:{chunk_b}"
            ),
            evidence_ids=[chunk_a, chunk_b],
            promotion_reasons=[],
        )
    )
    db.add(
        MemoryEntry(
            id="mem_a_only",
            entry_key="key_a_only",
            workspace_id=ws,
            version=1,
            is_current=1,
            content="Fiche mono-artefact A",
            status="accepted",
            source_artifact_ids=[art_a],
            origin_context=f"document:{art_a}:observation:obs_a:chunk:{chunk_a}",
            evidence_ids=[chunk_a],
            promotion_reasons=[],
        )
    )
    db.commit()

    compteurs = D.invalider_preuves_par_fragments(
        db, artifact_id=art_a, chunk_ids={chunk_a}, commit=False
    )
    db.flush()

    # La fiche partagée reste courante, ses preuves A retirées, B conservée.
    multi = (
        db.query(MemoryEntry)
        .filter(MemoryEntry.entry_key == "key_multi", MemoryEntry.is_current == 1)
        .one()
    )
    assert multi.status == "accepted"
    assert multi.evidence_ids == [chunk_b]

    # La fiche qui ne citait que A devient `stale`.
    mono = (
        db.query(MemoryEntry)
        .filter(MemoryEntry.entry_key == "key_a_only", MemoryEntry.is_current == 1)
        .one()
    )
    assert mono.status == "stale"
    assert mono.evidence_ids == []
    assert compteurs["mises_a_jour"] == 1
    assert compteurs["perimees"] == 1


def test_fenetre_suppression_chunks_rollback_si_invalidation_leve(db, monkeypatch):
    """🟡 C — une exception dans l'invalidation annule le remplacement."""
    ws = _workspace(db)
    art = _artifact(db)
    _lien(db, ws, art)
    texte_1 = "Le serveur utilise SQLite comme base de données depuis 2024."
    _chunks(db, art, texte_1)
    faits_1 = D.extraire_faits_document(
        db,
        artifact_id=art,
        workspace_id=ws,
        texte=texte_1,
        observation_id="obs_fail_1",
        extractor_version=D.VERSION_EXTRACTEUR,
        card_revision=1,
    )
    D.appliquer_faits_documentaires(
        db,
        workspace_id=ws,
        artifact_id=art,
        faits=faits_1,
        observation_id="obs_fail_1",
        extractor_version=D.VERSION_EXTRACTEUR,
    )
    anciens_chunks = {
        c.id for c in db.query(ArtifactChunk).filter_by(artifact_id=art).all()
    }
    assert faits_1[0].chunk_id in anciens_chunks
    fiche = (
        db.query(MemoryEntry)
        .filter(MemoryEntry.entry_key == faits_1[0].entry_key)
        .one()
    )
    statut_avant = fiche.status
    preuves_avant = list(fiche.evidence_ids or [])

    def _boom(*_a, **_k):
        raise RuntimeError("panne simulée d'invalidation")

    monkeypatch.setattr(D, "invalider_preuves_par_fragments", _boom)

    texte_2 = "La sauvegarde doit conserver 3 copies locales chaque nuit."
    with pytest.raises(RuntimeError, match="panne simulée"):
        _chunks(db, art, texte_2)

    # Le savepoint a annulé le delete : les anciens chunks restent en base.
    ids_apres = {c.id for c in db.query(ArtifactChunk).filter_by(artifact_id=art).all()}
    assert ids_apres == anciens_chunks

    # La fiche n'a pas été modifiée.
    fiche_apres = (
        db.query(MemoryEntry)
        .filter(MemoryEntry.entry_key == faits_1[0].entry_key)
        .one()
    )
    assert fiche_apres.status == statut_avant
    assert list(fiche_apres.evidence_ids or []) == preuves_avant
