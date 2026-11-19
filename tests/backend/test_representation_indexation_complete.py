"""Indexation et lecture sur la représentation COMPLÈTE (au-delà de 16 k).

Ce fichier verrouille le branchement de l'étape A dans les trois consommateurs
du texte documentaire :

  · l'indexation (`observations` → `chunks`) porte le Markdown complet, pas le
    `payload_ref` borné ;
  · `payload_ref` reste bornée à 16 k (contrainte de PROMPT, pas de lecture) ;
  · la lecture (`reader`) et `ask_files` préfèrent la représentation courante,
    avec repli sur l'observation pour les artefacts legacy.

Aucun modèle, aucun GPU : le découpage et les vecteurs sont neutralisés. La base
est neuve et jetable, l'index FTS et les tables de fragments sont réels.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, text as sql_text
from sqlalchemy.orm import sessionmaker

from backend.config.defaults import CHUNK_MAX_PAR_ARTEFACT
from backend.db import database as database_module
from backend.db.models import (
    Artifact,
    ArtifactCard,
    ArtifactChunk,
    ArtifactLink,
    ArtifactRepresentation,
    Base,
    Blob,
    Workspace,
)
from backend.ingestion import attachment_ingest, etat, representations
from backend.search import chunks as K
from backend.search import observations as O
from backend.search import reader as R
from backend.tools import ask_files as AF

SENTINELLE = "SENTINELLE-FIN-DU-DOCUMENT"


@contextmanager
def _session(sessions):
    db = sessions()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture()
def monde(tmp_path, monkeypatch):
    moteur = create_engine(f"sqlite:///{tmp_path / 'indexation-complete.db'}")
    Base.metadata.create_all(moteur)
    from backend.db.database import _ensure_fts

    _ensure_fts(moteur)
    sessions = sessionmaker(bind=moteur, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(database_module, "SessionLocal", sessions)
    # Pas de ColBERT : le FTS reste le juge du test, et la suite reste rapide.
    monkeypatch.setattr(K, "_indexer_fragment", lambda *_a, **_k: True)
    with _session(sessions) as db:
        K.ensure_chunk_tables(db)
        db.commit()
    return sessions


def _texte_long(taille_min: int = 100_000) -> str:
    """Un texte de phrases assez long, dont la sentinelle est APRÈS 16 k."""
    base = "La ligne de remplissage décrit un élément sans intérêt particulier. "
    repetitions = (taille_min // len(base)) + 50
    debut = base * repetitions
    assert len(debut) > taille_min
    return f"{debut[:-120]} {SENTINELLE} marque la fin du document."


def _artefact(
    sessions,
    *,
    kind: str = "text",
    state: str = etat.ETAT_EXTRAIT,
    with_link: bool = True,
) -> str:
    artifact_id = f"art_{uuid.uuid4().hex[:12]}"
    blob_id = f"blob_{artifact_id}"
    with _session(sessions) as db:
        db.add(
            Blob(
                id=blob_id,
                sha256=uuid.uuid4().hex,
                relative_path=f"blobs/{blob_id}.bin",
                size_bytes=1,
                mime_detected="text/plain",
                state="available",
            )
        )
        db.add(
            Artifact(
                id=artifact_id,
                blob_id=blob_id,
                kind=kind,
                original_filename=f"{artifact_id}.txt",
                display_name=f"{artifact_id}.txt",
                ingest_state=state,
            )
        )
        if with_link:
            workspace = f"ws_{uuid.uuid4().hex[:10]}"
            db.add(Workspace(id=workspace, name="Indexation complète"))
            db.add(
                ArtifactLink(
                    id=f"lnk_{uuid.uuid4().hex[:10]}",
                    artifact_id=artifact_id,
                    scope_kind="workspace",
                    scope_id=workspace,
                    role="shared",
                )
            )
        db.commit()
    return artifact_id


def _representation(sessions, artifact_id: str, texte: str) -> str:
    with _session(sessions) as db:
        representation = representations.creer_representation(
            db, artifact_id, texte, None, extractor_version="test/v1", commit=True
        )
        return representation.id


def _chunks(sessions, artifact_id: str) -> list[ArtifactChunk]:
    with _session(sessions) as db:
        return (
            db.query(ArtifactChunk)
            .filter(ArtifactChunk.artifact_id == artifact_id)
            .order_by(ArtifactChunk.ordinal.asc())
            .all()
        )


# ---------------------------------------------------------------------------
# 1. Le texte complet est indexé, jusqu'à la fin, et trouvé par le FTS
# ---------------------------------------------------------------------------


def test_le_texte_complet_est_indexe_et_trouve_apres_16k(monde):
    texte = _texte_long()
    assert len(texte) > 100_000
    assert texte.index(SENTINELLE) > 90_000

    artefact = _artefact(monde)
    representation_id = _representation(monde, artefact, texte)

    observation = O.record_extraction_observation(
        artefact,
        "attachment_ingest",
        None,
        texte[: O.PAYLOAD_CHARS],
        ok=True,
        tool_version="test/v1",
        representation_id=representation_id,
    )
    assert observation is not None

    # La preuve d'ingestion reste bornée : c'est le prompt, pas le document.
    assert len(observation.payload_ref or "") <= O.PAYLOAD_CHARS
    assert len(observation.payload_ref or "") == O.PAYLOAD_CHARS

    # Le lien représentation → observation est posé dans la même transaction.
    with _session(monde) as db:
        representation = db.get(ArtifactRepresentation, representation_id)
        assert representation.source_text_observation_id == observation.id

    fragments = _chunks(monde, artefact)
    assert len(fragments) >= 8
    assert any(SENTINELLE in fragment.text for fragment in fragments), (
        "aucun fragment ne porte la fin du document : l'indexation s'est arrêtée à 16 k"
    )

    # Tous les fragments portent leur provenance et leur empreinte.
    for fragment in fragments:
        assert fragment.representation_id == representation_id
        assert fragment.text_sha256 and len(fragment.text_sha256) == 64
        assert fragment.figures_revision == 0

    # Le FTS par fragment trouve une phrase située après 16 k.
    with _session(monde) as db:
        lignes = db.execute(
            sql_text(
                "SELECT artifact_id FROM chunk_search"
                " WHERE chunk_search MATCH :q AND artifact_id = :a"
            ),
            {"q": "sentinelle", "a": artefact},
        ).fetchall()
    assert [ligne[0] for ligne in lignes] == [artefact]

    # `best_source_text` préfère la représentation complète.
    with _session(monde) as db:
        art = db.get(Artifact, artefact)
        assert K.best_source_text(db, art) == texte


# ---------------------------------------------------------------------------
# 2. Lecture : la fin du document est lisible, et `page:N` découpe le Markdown
# ---------------------------------------------------------------------------


def test_read_document_sert_au_dela_de_16k_et_decoupe_les_pages(monde):
    page_1 = "Page un sans intérêt. " * 1000
    # La sentinelle est après 16 k, mais dans la page 2.
    assert len(page_1) > 16_000
    texte = f"## Page 1\n\n{page_1}\n\n## Page 2\n\n{SENTINELLE} sur la deuxième page."
    assert len(texte) > 16_000

    artefact = _artefact(monde, kind="pdf")
    _representation(monde, artefact, texte)

    with _session(monde) as db:
        art = db.get(Artifact, artefact)
        offset = R.read_payload(
            db,
            artefact,
            {artefact},
            locator=f"offset:{texte.index(SENTINELLE)}",
            max_chars=5000,
        )
        assert offset["ok"] is True
        assert SENTINELLE in offset["excerpt"]
        assert offset["total_chars"] == len(texte)

        page = R.read_payload(db, artefact, {artefact}, locator="page:2")
        assert page["ok"] is True
        assert SENTINELLE in page["excerpt"]
        assert page["locator_used"].startswith("page:2/")
        assert page["evidence_id"].startswith("rep:")

        hors_limites = R.read_payload(db, artefact, {artefact}, locator="page:9")
        assert hors_limites["ok"] is False


def test_ask_files_lit_la_representation_complete(monde):
    texte = _texte_long()
    artefact = _artefact(monde)
    _representation(monde, artefact, texte)

    with _session(monde) as db:
        art = db.get(Artifact, artefact)
        representation = AF._representation_ingeste(db, art)
        assert representation.motif_absence == ""
        assert SENTINELLE in representation.texte
        assert len(representation.texte) > 90_000

        reponse = AF._answer_one(
            "Que dit la sentinelle de fin ?", "doc_1", art, representation.texte, "text"
        )
        assert SENTINELLE in reponse["answer"]


# ---------------------------------------------------------------------------
# 3. Legacy : sans représentation, le repli reste borné à 16 k
# ---------------------------------------------------------------------------


def test_artefact_legacy_sans_representation_replie_sur_16k(monde):
    texte = _texte_long()
    artefact = _artefact(monde)  # aucune représentation

    observation = O.record_extraction_observation(
        artefact,
        "attachment_ingest",
        None,
        texte,
        ok=True,
        tool_version="test/legacy",
    )
    assert observation is not None
    assert len(observation.payload_ref or "") == O.PAYLOAD_CHARS

    fragments = _chunks(monde, artefact)
    assert fragments, "le repli legacy doit continuer d'indexer l'observation"
    assert not any(SENTINELLE in fragment.text for fragment in fragments), (
        "le repli legacy ne doit PAS dépasser 16 k"
    )
    assert all(fragment.representation_id is None for fragment in fragments)

    with _session(monde) as db:
        art = db.get(Artifact, artefact)
        returnu = K.best_source_text(db, art)
        assert len(returnu) <= O.PAYLOAD_CHARS
        assert SENTINELLE not in returnu


def test_indexation_complete_ne_double_pas_fiche_ni_enqueue(monde, monkeypatch):
    """Une seule révision de fiche, un seul enqueue, un réveil APRÈS le commit."""
    texte = _texte_long()
    artefact = _artefact(monde)
    representation_id = _representation(monde, artefact, texte)
    enqueues: list[str] = []
    reveils: list[bool] = []

    monkeypatch.setattr(
        O, "_reveiller_extraction_documentaire", lambda: reveils.append(True)
    )

    def hook(db, observation):
        assert (
            db.query(ArtifactChunk)
            .filter(ArtifactChunk.artifact_id == observation.artifact_id)
            .count()
            > 0
        ), "l'enqueue doit voir les fragments de la même transaction"
        enqueues.append(observation.id)

    observation = O.record_extraction_observation(
        artefact,
        "attachment_ingest",
        None,
        texte[: O.PAYLOAD_CHARS],
        ok=True,
        tool_version="test/v1",
        representation_id=representation_id,
        enqueue_documentaire=hook,
    )

    assert observation is not None
    assert enqueues == [observation.id]
    assert reveils == [True], "le réveil doit suivre le commit, une seule fois"

    with _session(monde) as db:
        cartes = (
            db.query(ArtifactCard).filter(ArtifactCard.artifact_id == artefact).all()
        )
        assert sum(1 for carte in cartes if carte.is_current == 1) == 1
        assert len(cartes) == 2, (
            "création + une seule révision pour l'observation (pas de double révision)"
        )


def test_une_observation_en_echec_ne_s_approprie_pas_la_representation(monde):
    texte = _texte_long()
    artefact = _artefact(monde)
    representation_id = _representation(monde, artefact, texte)

    O.record_extraction_observation(
        artefact,
        "attachment_ingest",
        None,
        "contenu refusé",
        ok=False,
        tool_version="test/echec",
        representation_id=representation_id,
    )

    with _session(monde) as db:
        representation = db.get(ArtifactRepresentation, representation_id)
        assert representation.source_text_observation_id is None


# ---------------------------------------------------------------------------
# 4. MAX_CHUNKS configurable : troncature EXPLICITE d'un côté, couverture totale
# ---------------------------------------------------------------------------


def test_max_chunks_est_configurable_et_la_troncature_est_explicite(
    monde, monkeypatch, caplog
):
    texte = _texte_long()
    artefact = _artefact(monde)
    representation_id = _representation(monde, artefact, texte)
    with _session(monde) as db:
        art = db.get(Artifact, artefact)
        with caplog.at_level(logging.WARNING, logger="backend.search.chunks"):
            court = K.ensure_chunks_for_artifact(
                db,
                art,
                texte,
                [("workspace", "ws_test")],
                representation_id=representation_id,
                max_chunks=2,
            )
        db.commit()
    assert court == 2
    assert any("troncature EXPLICITE" in message for message in caplog.messages)
    assert not any(SENTINELLE in f.text for f in _chunks(monde, artefact))

    with _session(monde) as db:
        art = db.get(Artifact, artefact)
        complet = K.ensure_chunks_for_artifact(
            db,
            art,
            texte,
            [("workspace", "ws_test")],
            representation_id=representation_id,
            max_chunks=500,
        )
        db.commit()
    assert complet > 2
    assert any(SENTINELLE in f.text for f in _chunks(monde, artefact))

    with _session(monde) as db:
        art = db.get(Artifact, artefact)
        with pytest.raises(ValueError, match="CHUNK_LIMIT"):
            K.ensure_chunks_for_artifact(db, art, texte, [], max_chunks=0)


def test_la_borne_par_defaut_couvre_un_document_de_100k():
    fragments = K.chunk_text(_texte_long())
    assert len(fragments) <= CHUNK_MAX_PAR_ARTEFACT
    # 100 k caractères ≈ 50 fragments de 2 000 : la borne 200 garde de la marge.
    assert len(fragments) < CHUNK_MAX_PAR_ARTEFACT
    assert SENTINELLE in fragments[-1]


# ---------------------------------------------------------------------------
# 5. Un fichier texte est lu EN ENTIER pour sa représentation
# ---------------------------------------------------------------------------


def test_texte_brut_ne_tronque_plus_a_16k(tmp_path):
    chemin = tmp_path / "long.txt"
    texte = _texte_long()
    chemin.write_text(texte, encoding="utf-8")

    rendu = attachment_ingest._texte_brut(str(chemin))

    assert SENTINELLE in rendu
    assert len(rendu) > 90_000


# ---------------------------------------------------------------------------
# 6. La lecture locale est bornée, et la troncature est dite
# ---------------------------------------------------------------------------


def test_texte_brut_tronque_a_la_borne_locale(tmp_path, caplog):
    """Un fichier de 2 M caractères est lu au plus jusqu'à MAX_CARACTERES_TEXTE.

    Sans cette borne, `fichier.read()` chargeait le fichier entier (seule
    l'upload 20 Mio limitait) avant de n'indexer que la tête. La troncature doit
    être JOURNALISÉE avec les deux nombres, pas silencieuse.
    """
    from backend.ingestion import markdown_pdf as MD

    chemin = tmp_path / "enorme.txt"
    chemin.write_text("A" * 2_000_000, encoding="utf-8")

    # Neutralise l'enveloppe Markdown pour mesurer la LECTURE, pas le rendu.
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        MD, "représentation_document", lambda _t, pages, **_k: pages[0].texte
    )
    try:
        with caplog.at_level(
            logging.WARNING, logger="backend.ingestion.attachment_ingest"
        ):
            rendu = attachment_ingest._texte_brut(str(chemin))
    finally:
        monkeypatch.undo()

    assert len(rendu) == K.MAX_CARACTERES_TEXTE
    assert "tronqué" in " ".join(caplog.messages)
    assert "MAX_CARACTERES_TEXTE" in " ".join(caplog.messages)


def test_ensure_chunks_tronque_avant_decoupage(monde, monkeypatch, caplog):
    """`chunk_text` ne voit jamais plus de MAX_CARACTERES_TEXTE caractères."""
    texte = "Phrase courte sans intérêt. " * 100_000  # ≈ 2,8 M caractères
    assert len(texte) > K.MAX_CARACTERES_TEXTE

    vus: list[int] = []
    original = K.chunk_text
    monkeypatch.setattr(K, "chunk_text", lambda t: vus.append(len(t)) or original(t))
    monkeypatch.setattr(K, "_indexer_fragments", lambda *_a, **_k: 0)

    artefact = _artefact(monde)
    with _session(monde) as db:
        art = db.get(Artifact, artefact)
        with caplog.at_level(logging.WARNING, logger="backend.search.chunks"):
            nombre = K.ensure_chunks_for_artifact(
                db, art, texte, [("workspace", "ws_test")], max_chunks=5
            )
        db.commit()

    assert nombre == 5
    assert vus and vus[0] <= K.MAX_CARACTERES_TEXTE
    assert "tronqué" in " ".join(caplog.messages)


# ---------------------------------------------------------------------------
# 7. Réindexation CONDITIONNELLE : une lecture ne refait pas l'index
# ---------------------------------------------------------------------------


def test_une_lecture_sur_artefact_deja_indexe_ne_reindexe_pas(monde, monkeypatch):
    """Même ids, même nombre de fragments, aucun réencodage.

    `read_document`/`doc_ocr`/`read_text` créent une observation append-only :
    sans cette garde, chaque lecture réussie refaisait une révision de fiche,
    de nouveaux ids et un encodage ColBERT complet. Les fragments existants
    doivent rester intacts.
    """
    texte = _texte_long()
    artefact = _artefact(monde)
    representation_id = _representation(monde, artefact, texte)

    O.record_extraction_observation(
        artefact,
        "attachment_ingest",
        None,
        texte[: O.PAYLOAD_CHARS],
        ok=True,
        tool_version="test/v1",
        representation_id=representation_id,
    )
    avant = [(c.id, c.ordinal, c.text_sha256) for c in _chunks(monde, artefact)]
    assert avant

    appels: list[int] = []
    original = K.ensure_chunks_for_artifact
    monkeypatch.setattr(
        K,
        "ensure_chunks_for_artifact",
        lambda *a, **k: appels.append(1) or original(*a, **k),
    )

    observation = O.record_extraction_observation(
        artefact,
        "read_document",
        None,
        "extrait localisé",
        ok=True,
        tool_version="test/read",
    )
    assert observation is not None
    assert appels == [], "une lecture sur un artefact déjà indexé ne doit pas réindexer"
    assert [(c.id, c.ordinal, c.text_sha256) for c in _chunks(monde, artefact)] == avant


def test_le_repli_legacy_n_a_lieu_qu_une_fois(monde, monkeypatch):
    """Sans représentation, le repli 16 k ne se rejoue pas à chaque lecture."""
    texte = _texte_long()
    artefact = _artefact(monde)  # aucune représentation

    O.record_extraction_observation(
        artefact,
        "attachment_ingest",
        None,
        texte,
        ok=True,
        tool_version="test/legacy",
    )
    assert _chunks(monde, artefact)

    appels: list[int] = []
    original = K.ensure_chunks_for_artifact
    monkeypatch.setattr(
        K,
        "ensure_chunks_for_artifact",
        lambda *a, **k: appels.append(1) or original(*a, **k),
    )

    O.record_extraction_observation(
        artefact,
        "read_document",
        None,
        "relecture",
        ok=True,
        tool_version="test/read",
    )
    assert appels == [], "le repli legacy ne doit pas se rejouer à chaque lecture"


def test_une_nouvelle_ingestion_avec_representation_indexe_le_texte_complet(monde):
    """Une représentation EXPLICITE refait bien l'index complet, même après une lecture."""
    texte = _texte_long()
    artefact = _artefact(monde)

    # Première ingestion : indexation legacy bornée à 16 k.
    O.record_extraction_observation(
        artefact,
        "attachment_ingest",
        None,
        texte,
        ok=True,
        tool_version="test/legacy",
    )
    assert _chunks(monde, artefact)
    assert not any(SENTINELLE in f.text for f in _chunks(monde, artefact))

    # Nouvelle représentation d'ingestion, explicitement fournie : index complet.
    representation_id = _representation(monde, artefact, texte)
    O.record_extraction_observation(
        artefact,
        "attachment_ingest",
        None,
        texte[: O.PAYLOAD_CHARS],
        ok=True,
        tool_version="test/v2",
        representation_id=representation_id,
    )
    assert any(SENTINELLE in f.text for f in _chunks(monde, artefact))


def test_les_fragments_sont_ajoutes_en_batch(monde, monkeypatch):
    """Un seul appel d'encodage pour tout le lot, jamais un par fragment."""
    texte = _texte_long()
    artefact = _artefact(monde)
    representation_id = _representation(monde, artefact, texte)

    appels: list[int] = []
    monkeypatch.setattr(
        K, "_indexer_fragments", lambda _s, frags: appels.append(len(frags)) or 0
    )

    def interdit(*_a, **_k):
        raise AssertionError(
            "`_indexer_fragment` ne doit plus être appelé par fragment"
        )

    monkeypatch.setattr(K, "_indexer_fragment", interdit)

    with _session(monde) as db:
        art = db.get(Artifact, artefact)
        nombre = K.ensure_chunks_for_artifact(
            db,
            art,
            texte,
            [("workspace", "ws_test")],
            representation_id=representation_id,
        )
        db.commit()

    assert nombre > 1
    assert appels == [nombre], "tout le lot doit être encodé en UN appel"


# ---------------------------------------------------------------------------
# 8. États sans texte : `ask_files` ne sert pas une représentation périmée
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", [etat.ETAT_BINAIRE, etat.ETAT_VIDE])
def test_ask_files_refuse_une_representation_si_etat_sans_texte(monde, state):
    texte = _texte_long()
    artefact = _artefact(monde, state=state)
    _representation(monde, artefact, texte)

    with _session(monde) as db:
        art = db.get(Artifact, artefact)
        representation = AF._representation_ingeste(db, art)
        assert representation.texte == ""
        assert representation.motif_absence != ""


@pytest.mark.parametrize(
    "state", [etat.ETAT_EXTRAIT, etat.ETAT_TRONQUE, etat.ETAT_PARTIEL]
)
def test_ask_files_sert_la_representation_si_etat_porte_du_texte(monde, state):
    texte = _texte_long()
    artefact = _artefact(monde, state=state)
    _representation(monde, artefact, texte)

    with _session(monde) as db:
        art = db.get(Artifact, artefact)
        representation = AF._representation_ingeste(db, art)
        assert SENTINELLE in representation.texte


# ---------------------------------------------------------------------------
# 9. Lien de provenance par le vrai chemin de persistance d'ingestion
# ---------------------------------------------------------------------------


def test_lien_source_text_observation_via_persister_extraction(monde):
    texte = _texte_long()
    artefact = _artefact(monde)

    representation = attachment_ingest._persister_extraction_complete(
        artefact, texte, None
    )
    assert representation is not None
    assert representation.source_text_observation_id is None

    observation = O.record_extraction_observation(
        artefact,
        "attachment_ingest",
        None,
        texte[: O.PAYLOAD_CHARS],
        ok=True,
        tool_version="test/v1",
        representation_id=representation.id,
    )
    assert observation is not None
    with _session(monde) as db:
        relu = db.get(ArtifactRepresentation, representation.id)
        assert relu is not None
        assert relu.source_text_observation_id == observation.id


# ---------------------------------------------------------------------------
# 10. La lecture INTERNE peut dépasser 16 k, mais reste bornée à 200 k
# ---------------------------------------------------------------------------


def test_executeur_read_document_borne_max_chars_a_16k(monkeypatch):
    """Le boundary OUTIL applique 16 k, même si l'appelant demande 200 k.

    `ReadDocumentArgs.max_chars` déclare `le=16000`, mais `loop.py` appelle
    `reader.read_payload` directement, sans passer par pydantic. Sans ce
    plafond, le chemin outil pouvait demander la borne INTERNE de 200 k.
    """
    from backend.inference import loop as loopmod

    vus: dict[str, int] = {}

    def faux_read_payload(_db, _artifact_id, _allowed, **kwargs):
        vus["max_chars"] = int(kwargs.get("max_chars") or 0)
        return {"ok": False, "code": "TEST", "message": "refus de test"}

    class FauxDb:
        def close(self):
            pass

    monkeypatch.setattr("backend.search.reader.read_payload", faux_read_payload)
    monkeypatch.setattr(
        "backend.search.scope.resolve_allowed_artifacts", lambda _c: set()
    )
    monkeypatch.setattr("backend.db.database.SessionLocal", lambda: FauxDb())

    resultat = loopmod._execute_read_document(
        {"args": {"artifact_id": "art_test", "max_chars": 200_000}}
    )
    assert resultat["ok"] is False
    assert vus["max_chars"] == 16_000


def test_reader_sert_plus_de_16k_mais_borne_a_200k(monde):
    texte = _texte_long(300_000)
    assert len(texte) > 200_000

    artefact = _artefact(monde)
    _representation(monde, artefact, texte)

    with _session(monde) as db:
        payload = R.read_payload(db, artefact, {artefact}, max_chars=200_000)
        assert payload["ok"] is True
        assert len(payload["excerpt"]) > 16_000
        assert payload["total_chars"] == len(texte)

        plafonne = R.read_payload(db, artefact, {artefact}, max_chars=10_000_000)
        assert len(plafonne["excerpt"]) <= 200_000
