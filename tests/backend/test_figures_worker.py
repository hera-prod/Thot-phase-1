"""Étape B1 — worker figures : claim, crop, description, patch et audit.

Ces tests n'appellent AUCUN modèle réel. Le PDF est un vrai fichier PyMuPDF ;
la vision est un double qui rend une description déterministe. Ils verrouillent
la file `artifact_figures`, le patch de la représentation courante et le
caractère audit-only de l'observation `attachment_figures`.
"""

from __future__ import annotations

import json
import threading
import uuid
from contextlib import contextmanager
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, text as sql_text
from sqlalchemy.orm import sessionmaker

from backend.db import database as database_module
from backend.db.models import (
    Artifact,
    ArtifactCard,
    ArtifactChunk,
    ArtifactFigure,
    ArtifactLink,
    ArtifactRepresentation,
    Base,
    Blob,
    MemoryDocumentExtraction,
    Observation,
)
from backend.ingestion import (
    figures_etape,
    figures_pdf,
    figures_store,
    figures_worker,
    representations,
    vision_appel,
)
from backend.ingestion.etat import utc_naif
from backend.ipc import paths_bridge as paths_bridge_module
from backend.search import chunks as chunks_module

ECHELLE = 200 / 72


@contextmanager
def _session(sessions):
    db = sessions()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture()
def monde(tmp_path, monkeypatch):
    moteur = create_engine(
        f"sqlite:///{tmp_path / 'figures-b1.db'}",
        connect_args={"timeout": 20},
    )
    Base.metadata.create_all(moteur)
    from backend.db.database import _ensure_fts
    from backend.search.chunks import ensure_chunk_tables

    _ensure_fts(moteur)
    sessions = sessionmaker(bind=moteur, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(database_module, "SessionLocal", sessions)
    racine = tmp_path / "data"
    (racine / "blobs").mkdir(parents=True)
    (racine / "temp").mkdir(parents=True)
    monkeypatch.setenv("THOT_PORTABLE", "1")
    monkeypatch.setenv("THOT_APP_DATA_DIR", str(racine))
    monkeypatch.setattr(
        paths_bridge_module,
        "data_dirs",
        lambda: (racine, racine / "temp"),
        raising=False,
    )
    monkeypatch.setattr(
        paths_bridge_module, "blobs_root", lambda: racine / "blobs", raising=False
    )
    with _session(sessions) as db:
        ensure_chunk_tables(db)
        db.commit()
    figures_worker._stop.clear()
    figures_worker._reveil.clear()
    yield sessions, racine


def _pdf_avec_figure(chemin) -> None:
    import pymupdf

    source = pymupdf.open()
    page = source.new_page(width=595, height=842)
    page.insert_text((50, 60), "TEXTE-DE-PAGE-B1", fontsize=14)
    page.draw_rect(pymupdf.Rect(120, 120, 320, 320), color=(0, 0, 0), fill=(1, 0, 0))
    pixmap = page.get_pixmap(dpi=150)
    cible = pymupdf.open()
    page_cible = cible.new_page(width=595, height=842)
    page_cible.insert_image(page_cible.rect, stream=pixmap.tobytes("png"))
    cible.save(str(chemin))
    source.close()
    cible.close()


def _boite() -> list[float]:
    return [
        round(120 * ECHELLE, 1),
        round(120 * ECHELLE, 1),
        round(320 * ECHELLE, 1),
        round(320 * ECHELLE, 1),
    ]


def _creer_document(
    sessions,
    racine,
    *,
    bbox: list[float] | None = None,
    nom: str = "figure.pdf",
) -> tuple[str, str, str]:
    """Crée blob + artefact + représentation courante + figure pending.

    Rend (artifact_id, representation_id, figure_id).
    """
    chemin = racine / nom
    _pdf_avec_figure(chemin)
    marque = uuid.uuid4().hex[:10]
    artifact_id = f"art_{marque}"
    blob_id = f"blob_{marque}"
    relatif = f"blobs/{blob_id}.pdf"
    (racine / relatif).write_bytes(chemin.read_bytes())

    placeholder = figures_pdf.bloc_figure_en_attente(1, 1)
    markdown = f"TEXTE-DE-PAGE-B1\n\n{placeholder}\n\nFIN"
    blocs = [
        {
            "page": 1,
            "region": None,
            "type": "texte",
            "bbox": None,
            "ordre": 0,
            "contenu": "TEXTE-DE-PAGE-B1",
        },
        {
            "page": 1,
            "region": 1,
            "type": "figure",
            "bbox": _boite(),
            "ordre": 1,
            "contenu": placeholder,
        },
    ]

    with _session(sessions) as db:
        db.add(
            Blob(
                id=blob_id,
                sha256=marque,
                relative_path=relatif,
                size_bytes=1,
                mime_detected="application/pdf",
            )
        )
        db.add(
            Artifact(
                id=artifact_id,
                blob_id=blob_id,
                kind="pdf",
                original_filename=nom,
                display_name=nom,
                ingest_state="extrait",
            )
        )
        db.commit()
        representation = representations.creer_representation(
            db, artifact_id, markdown, blocs, extractor_version="test/b1", commit=False
        )
        figures_store.enregistrer_figures_en_attente(
            db,
            artifact_id,
            representation.id,
            [
                figures_pdf.FigurePlaceholder(
                    page=1,
                    region=1,
                    label="figure",
                    score=0.9,
                    bbox=tuple(bbox or _boite()),
                )
            ],
            extractor_version="test/b1",
        )
        figures_store.mettre_a_jour_etat_figures(db, artifact_id)
        db.commit()
        figure = (
            db.query(ArtifactFigure)
            .filter(ArtifactFigure.artifact_id == artifact_id)
            .one()
        )
        return artifact_id, representation.id, figure.id


def _reclamer(sessions):
    with _session(sessions) as db:
        return figures_etape.reclamer_figure(db)


def _executer(claim):
    figures_worker._executer(claim)


def _figure(sessions, figure_id):
    with _session(sessions) as db:
        return db.get(ArtifactFigure, figure_id)


def _representations_et_artefact(sessions, artifact_id, representation_id):
    with _session(sessions) as db:
        return (
            db.get(ArtifactRepresentation, representation_id),
            db.get(Artifact, artifact_id),
        )


def _poser_fragments(
    sessions,
    artifact_id: str,
    representation_id: str | None,
    textes: list[str],
    *,
    card_revision: int = 7,
) -> list[str]:
    """Pose des fragments + FTS + vecteurs, comme le ferait l'indexation.

    Les vecteurs sont des octets factices : ces tests vérifient la LIGNE
    supprimée ou conservée, jamais l'encodage ColBERT (aucun GPU).
    """
    ids: list[str] = []
    with _session(sessions) as db:
        for ordinal, texte in enumerate(textes):
            chunk_id = f"chk_{artifact_id}_{ordinal}"
            ids.append(chunk_id)
            db.add(
                ArtifactChunk(
                    id=chunk_id,
                    artifact_id=artifact_id,
                    representation_id=representation_id,
                    ordinal=ordinal,
                    card_revision=card_revision,
                    text=texte,
                    context="contexte de test",
                    text_sha256=chunks_module._sha256_texte(texte),
                    figures_revision=0,
                )
            )
            db.flush()
            db.execute(
                sql_text(
                    """INSERT INTO chunk_search(
                           chunk_id, artifact_id, scope_kind, scope_id, text, context)
                       VALUES (:c, :a, 'workspace', 'ws_test', :t, :x)"""
                ),
                {"c": chunk_id, "a": artifact_id, "t": texte, "x": "contexte de test"},
            )
            db.execute(
                sql_text(
                    """INSERT INTO artifact_chunk_vectors(chunk_id, dim, vector)
                       VALUES (:c, 1, :v)"""
                ),
                {"c": chunk_id, "v": b"vecteur-factice"},
            )
        db.commit()
    return ids


# ---------------------------------------------------------------------------
# 1. Claim atomique et reprise d'un running expiré
# ---------------------------------------------------------------------------


def test_claim_atomique_deux_workers_ne_prennent_pas_la_meme_figure(monde):
    sessions, _ = monde
    _, _, figure_id = _creer_document(sessions, monde[1])
    barriere = threading.Barrier(2)
    resultats: list[str | None] = []

    def _course():
        from backend.db.database import SessionLocal

        barriere.wait()
        db = SessionLocal()
        try:
            claim = figures_etape.reclamer_figure(db)
            resultats.append(claim.figure_id if claim else None)
        except Exception:
            resultats.append(None)
            raise
        finally:
            db.close()

    fils = [threading.Thread(target=_course) for _ in range(2)]
    for fil in fils:
        fil.start()
    for fil in fils:
        fil.join(10)

    assert sorted(r for r in resultats if r) == [figure_id], resultats
    assert resultats.count(None) == 1, "un seul worker doit gagner le claim"
    figure = _figure(sessions, figure_id)
    assert figure.status == figures_store.STATUT_RUNNING
    assert figure.attempts == 1
    assert figure.claim_token
    with _session(sessions) as db:
        artefact = db.get(Artifact, figure.artifact_id)
        assert artefact.figures_state == figures_store.ETAT_FIGURES_EN_COURS


def test_running_expire_est_repris_avec_un_nouveau_token(monde):
    sessions, _ = monde
    _, _, figure_id = _creer_document(sessions, monde[1])
    premier = _reclamer(sessions)
    assert premier is not None

    with _session(sessions) as db:
        ligne = db.get(ArtifactFigure, figure_id)
        ligne.claimed_at = utc_naif() - timedelta(seconds=figures_etape.TTL_CLAIM_S + 1)
        db.commit()

    second = _reclamer(sessions)
    assert second is not None
    assert second.figure_id == figure_id
    assert second.claim_token != premier.claim_token
    assert second.attempts == 2
    figure = _figure(sessions, figure_id)
    assert figure.claim_token == second.claim_token


# ---------------------------------------------------------------------------
# 2. Description réussie : patch Markdown/blocs, statut, compteurs
# ---------------------------------------------------------------------------


def test_description_reussie_patche_markdown_blocs_et_compteurs(monde, monkeypatch):
    sessions, racine = monde
    artifact_id, representation_id, figure_id = _creer_document(sessions, racine)
    monkeypatch.setattr(
        "backend.ingestion.vision_appel.appeler_vision_generale",
        lambda consigne, pixels: "Un rectangle rouge uni.",
    )

    claim = _reclamer(sessions)
    assert claim is not None
    _executer(claim)

    figure = _figure(sessions, figure_id)
    representation, artifact = _representations_et_artefact(
        sessions, artifact_id, representation_id
    )

    assert figure.status == figures_store.STATUT_DONE
    assert figure.description == "Un rectangle rouge uni."
    assert figure.description_sha256 == representations.sha256_texte(
        "Un rectangle rouge uni."
    )
    assert figure.applied_revision == 1
    assert figure.done_at is not None
    assert figure.last_error is None
    assert (
        figure.crop_relpath
        == f"derived/figures/{figure.crop_sha256[:2]}/{figure.crop_sha256}.png"
    )
    assert (racine / figure.crop_relpath).exists()
    assert figure.crop_width > 0 and figure.crop_height > 0

    assert "description en attente" not in representation.markdown
    assert (
        "**Figure** (page 1 · région 1) : Un rectangle rouge uni."
        in representation.markdown
    )
    assert representation.figures_revision == 1
    assert representation.markdown_sha256 == representations.sha256_texte(
        representation.markdown
    )
    assert representation.chars_total == len(representation.markdown)
    assert set(json.loads(representation.blocks_json)[1]) >= {
        "page",
        "region",
        "type",
        "contenu",
    }
    assert "Un rectangle rouge uni." in representation.blocks_json
    assert artifact.figures_state == figures_store.ETAT_FIGURES_COMPLET
    assert artifact.figures_done == 1
    assert artifact.figures_total == 1


# ---------------------------------------------------------------------------
# 3. Observation d'audit : append-only, sans fiche/chunks/mémoire
# ---------------------------------------------------------------------------


def test_observation_attachment_figures_audit_only(monde, monkeypatch):
    sessions, racine = monde
    artifact_id, representation_id, figure_id = _creer_document(sessions, racine)

    from backend.ingestion import etat as etat_ingestion
    from backend.search import observations as obs_module

    assert "attachment_figures" not in obs_module.OUTILS_EXTRACTION
    assert "attachment_figures" not in etat_ingestion.OUTILS_EXTRACTION

    with _session(sessions) as db:
        db.add(
            ArtifactCard(
                id="card_avant",
                artifact_id=artifact_id,
                revision=1,
                is_current=1,
                summary="résumé initial",
            )
        )
        db.add(
            ArtifactChunk(
                id="chk_avant",
                artifact_id=artifact_id,
                representation_id=representation_id,
                ordinal=0,
                text="texte indexé initial",
                text_sha256="sha-avant",
            )
        )
        db.commit()

    monkeypatch.setattr(
        "backend.ingestion.vision_appel.appeler_vision_generale",
        lambda consigne, pixels: "Une courbe décroissante.",
    )
    _executer(_reclamer(sessions))

    with _session(sessions) as db:
        observation = (
            db.query(Observation)
            .filter(
                Observation.artifact_id == artifact_id,
                Observation.tool_name == "attachment_figures",
            )
            .one()
        )
        assert observation.status == "completed"
        assert (
            observation.input_fingerprint and len(observation.input_fingerprint) == 16
        )
        resultat = json.loads(observation.result_json)
        assert resultat["figure_id"] == figure_id
        assert resultat["crop_sha256"]
        assert resultat["prompt_version"] == figures_pdf.PROMPT_FIGURE_REEXAMINEE

        # Aucune nouvelle révision de fiche, aucun chunk réécrit, aucun enqueue.
        assert (
            db.query(ArtifactCard)
            .filter(ArtifactCard.artifact_id == artifact_id)
            .count()
            == 1
        )
        fiche = (
            db.query(ArtifactCard).filter(ArtifactCard.artifact_id == artifact_id).one()
        )
        assert fiche.revision == 1
        assert fiche.summary == "résumé initial"
        assert (
            db.query(ArtifactChunk)
            .filter(ArtifactChunk.artifact_id == artifact_id)
            .count()
            == 1
        )
        chunk = db.query(ArtifactChunk).filter(ArtifactChunk.id == "chk_avant").one()
        assert chunk.text == "texte indexé initial"
        assert (
            db.query(MemoryDocumentExtraction)
            .filter(MemoryDocumentExtraction.artifact_id == artifact_id)
            .count()
            == 0
        )
        figure = db.get(ArtifactFigure, figure_id)
        assert figure.description_observation_id == observation.id


# ---------------------------------------------------------------------------
# 4. VLM indisponible : texte intact, différé sans consommer d'essai
# ---------------------------------------------------------------------------


def test_vlm_indisponible_texte_intact_et_retry_possible(monde, monkeypatch):
    sessions, racine = monde
    artifact_id, representation_id, figure_id = _creer_document(sessions, racine)

    def _indisponible(consigne, pixels):
        raise vision_appel.VisionIndisponible("aucune couche d'appel")

    monkeypatch.setattr(
        "backend.ingestion.vision_appel.appeler_vision_generale", _indisponible
    )
    claim = _reclamer(sessions)
    _executer(claim)

    figure = _figure(sessions, figure_id)
    representation, artifact = _representations_et_artefact(
        sessions, artifact_id, representation_id
    )
    assert figure.status == figures_store.STATUT_PENDING
    assert figure.attempts == 0, "un différé ne consomme pas d'essai"
    assert figure.last_error is None, "un différé n'est pas un échec"
    assert figure.next_attempt_at is not None
    assert "description en attente" in representation.markdown
    assert artifact.figures_state == figures_store.ETAT_FIGURES_EN_ATTENTE

    # Retry : on rend la figure due, puis la vision répond.
    with _session(sessions) as db:
        ligne = db.get(ArtifactFigure, figure_id)
        ligne.next_attempt_at = utc_naif() - timedelta(seconds=1)
        db.commit()
    monkeypatch.setattr(
        "backend.ingestion.vision_appel.appeler_vision_generale",
        lambda consigne, pixels: "Une photographie de bâtiment.",
    )
    second = _reclamer(sessions)
    assert second is not None
    _executer(second)

    assert _figure(sessions, figure_id).status == figures_store.STATUT_DONE
    _, artifact = _representations_et_artefact(sessions, artifact_id, representation_id)
    assert artifact.figures_state == figures_store.ETAT_FIGURES_COMPLET


# ---------------------------------------------------------------------------
# 5. Réponse vide / invalide : limite terminale, pas de perte silencieuse
# ---------------------------------------------------------------------------


def test_description_vide_marque_limite_terminale(monde, monkeypatch):
    sessions, racine = monde
    artifact_id, representation_id, figure_id = _creer_document(sessions, racine)

    def _vide(consigne, pixels):
        raise vision_appel.VisionIndisponible(
            "vision générale : réponse vide (b64_len=42) — aucun texte rendu"
        )

    monkeypatch.setattr("backend.ingestion.vision_appel.appeler_vision_generale", _vide)
    _executer(_reclamer(sessions))

    figure = _figure(sessions, figure_id)
    representation, artifact = _representations_et_artefact(
        sessions, artifact_id, representation_id
    )
    assert figure.status == figures_store.STATUT_LIMITE
    assert figure.last_error and "réponse vide" in figure.last_error
    assert figure.description is None
    assert artifact.figures_state == figures_store.ETAT_FIGURES_LIMITE
    assert "réexamen indisponible" in representation.markdown
    assert figure.next_attempt_at is None


# ---------------------------------------------------------------------------
# 6. Crop introuvable / layout drift : limite motivée, aucune description
# ---------------------------------------------------------------------------


def test_layout_drift_marque_limite_sans_decrire_une_mauvaise_zone(monde, monkeypatch):
    sessions, racine = monde
    artifact_id, representation_id, figure_id = _creer_document(
        sessions, racine, bbox=[5000.0, 5000.0, 5100.0, 5100.0]
    )
    monkeypatch.setattr(
        "backend.ingestion.vision_appel.appeler_vision_generale",
        lambda consigne, pixels: pytest.fail(
            "aucune description ne doit être produite sur une zone introuvable"
        ),
    )

    _executer(_reclamer(sessions))

    figure = _figure(sessions, figure_id)
    representation, artifact = _representations_et_artefact(
        sessions, artifact_id, representation_id
    )
    assert figure.status == figures_store.STATUT_LIMITE
    assert figure.description is None
    assert figure.last_error and "layout drift" in figure.last_error.lower()
    assert artifact.figures_state == figures_store.ETAT_FIGURES_LIMITE
    assert artifact.figures_limited == 1
    assert "réexamen indisponible" in representation.markdown
    assert "Un rectangle rouge" not in representation.markdown


# ---------------------------------------------------------------------------
# 6. Priorité texte : la file d'admission non vide fait différer le worker
# ---------------------------------------------------------------------------


def test_file_texte_non_vide_fait_differer_le_worker(monde):
    sessions, _ = monde
    _, _, figure_id = _creer_document(sessions, monde[1])
    from backend.ingestion import attachment_ingest

    attachment_ingest._file_admission.put(("art-en-attente", True))
    attachment_ingest._arret_admission.clear()
    try:
        figures_worker._traiter_pending(threading.Event())
    finally:
        try:
            attachment_ingest._file_admission.get_nowait()
        except Exception:
            pass

    figure = _figure(sessions, figure_id)
    assert figure.status == figures_store.STATUT_PENDING
    assert figure.attempts == 0
    assert figure.claim_token is None


# ---------------------------------------------------------------------------
# 6 bis. Priorité chat : aucun verrou global, aucun claim pendant un tour
# ---------------------------------------------------------------------------


def test_chat_actif_differe_sans_claim_ni_essai(monde):
    sessions, racine = monde
    artifact_id, representation_id, figure_id = _creer_document(sessions, racine)
    from backend.services import activity

    activity.marquer_tour_actif(True)
    try:
        figures_worker._traiter_pending(threading.Event())
    finally:
        activity.marquer_tour_actif(False)

    figure = _figure(sessions, figure_id)
    _, artifact = _representations_et_artefact(sessions, artifact_id, representation_id)
    assert figure.status == figures_store.STATUT_PENDING
    assert figure.attempts == 0, "un chat actif ne consomme aucun essai"
    assert figure.claim_token is None, "aucune figure ne doit être réclamée"
    assert artifact.figures_state == figures_store.ETAT_FIGURES_EN_ATTENTE


def test_chat_inactif_traite_la_figure(monde, monkeypatch):
    sessions, racine = monde
    artifact_id, representation_id, figure_id = _creer_document(sessions, racine)
    from backend.services import activity

    activity.marquer_tour_actif(False)
    monkeypatch.setattr(
        "backend.ingestion.vision_appel.appeler_vision_generale",
        lambda consigne, pixels: "Une figure décrite après le tour.",
    )

    figures_worker._traiter_pending(threading.Event())

    figure = _figure(sessions, figure_id)
    _, artifact = _representations_et_artefact(sessions, artifact_id, representation_id)
    assert figure.status == figures_store.STATUT_DONE
    assert figure.attempts == 1
    assert figure.description == "Une figure décrite après le tour."
    assert artifact.figures_state == figures_store.ETAT_FIGURES_COMPLET


@pytest.mark.parametrize(
    "module, erreur",
    [
        ("backend.inference.resource_coordinator", "ResourceBusy"),
        ("backend.ingestion.paddle_client", "ModeleIndisponible"),
    ],
)
def test_indisponibilite_pendant_redetection_differe_sans_limite(
    monde, monkeypatch, module, erreur
):
    sessions, racine = monde
    artifact_id, representation_id, figure_id = _creer_document(sessions, racine)

    # Sans bbox, le worker re-détecte : c'est le chemin où une contention GPU/OCR
    # ne doit pas devenir une « limite » terminale.
    with _session(sessions) as db:
        ligne = db.get(ArtifactFigure, figure_id)
        ligne.bbox_json = None
        db.commit()

    import importlib

    classe = getattr(importlib.import_module(module), erreur)

    def _indisponible(png, **kwargs):
        raise classe("ressource momentanément occupée")

    monkeypatch.setattr("backend.ingestion.paddle_client.detect_regions", _indisponible)
    monkeypatch.setattr(
        "backend.ingestion.vision_appel.appeler_vision_generale",
        lambda *a, **k: pytest.fail(
            "une indisponibilité transitoire n'appelle pas le VLM"
        ),
    )

    _executer(_reclamer(sessions))

    figure = _figure(sessions, figure_id)
    representation, artifact = _representations_et_artefact(
        sessions, artifact_id, representation_id
    )
    assert figure.status == figures_store.STATUT_PENDING
    assert figure.attempts == 0, "un différé anti-compte le claim"
    assert figure.last_error is None, "un différé n'est pas un échec"
    assert figure.next_attempt_at is not None
    assert artifact.figures_state == figures_store.ETAT_FIGURES_EN_ATTENTE
    assert artifact.figures_state != figures_store.ETAT_FIGURES_LIMITE
    assert artifact.figures_limited == 0
    assert "description en attente" in representation.markdown
    assert "réexamen indisponible" not in representation.markdown


# ---------------------------------------------------------------------------
# 7. Représentation superseded : lot abandonné, aucun patch
# ---------------------------------------------------------------------------


def test_representation_superseded_abandonne_sans_patcher(monde, monkeypatch):
    sessions, racine = monde
    artifact_id, ancienne_id, figure_id = _creer_document(sessions, racine)
    claim = _reclamer(sessions)
    assert claim is not None

    # Une ré-ingestion périme la représentation courante et ses figures.
    with _session(sessions) as db:
        representations.creer_representation(
            db,
            artifact_id,
            "NOUVELLE-REPRESENTATION",
            [],
            extractor_version="test/reingestion",
            commit=True,
        )
    monkeypatch.setattr(
        "backend.ingestion.vision_appel.appeler_vision_generale",
        lambda consigne, pixels: pytest.fail("une version périmée ne se décrit pas"),
    )

    _executer(claim)

    with _session(sessions) as db:
        ancienne = db.get(ArtifactRepresentation, ancienne_id)
        figure = db.get(ArtifactFigure, figure_id)
        assert ancienne.markdown.count("description en attente") == 1
        assert ancienne.figures_revision == 0
        assert figure.status == figures_store.STATUT_SUPERSEDED
        assert figure.description is None


# ---------------------------------------------------------------------------
# 8. Étape B2 — la description devient cherchable (fragments + FTS + vecteurs)
# ---------------------------------------------------------------------------


def test_description_patche_le_fragment_fts_et_vecteur_sans_reindexer(
    monde, monkeypatch
):
    sessions, racine = monde
    artifact_id, representation_id, figure_id = _creer_document(sessions, racine)
    attente = figures_pdf.bloc_figure_en_attente(1, 1)
    ids = _poser_fragments(
        sessions,
        artifact_id,
        representation_id,
        [f"DEBUT-DU-FRAGMENT\n\n{attente}\n\nFIN-DU-FRAGMENT", "AUTRE-FRAGMENT"],
    )
    avant = {}
    with _session(sessions) as db:
        for chunk_id in ids:
            chunk = db.get(ArtifactChunk, chunk_id)
            avant[chunk_id] = (chunk.text, chunk.text_sha256)
        db.add(
            ArtifactCard(
                id=f"card_{artifact_id}",
                artifact_id=artifact_id,
                revision=1,
                is_current=1,
                summary="résumé initial",
            )
        )
        db.commit()

    description = "Un kaléidoscope vermillon décrit la figure."
    monkeypatch.setattr(
        "backend.ingestion.vision_appel.appeler_vision_generale",
        lambda consigne, pixels: description,
    )
    appels: list[int] = []
    monkeypatch.setattr(
        chunks_module,
        "ensure_chunks_for_artifact",
        lambda *a, **k: appels.append(1),
    )

    _executer(_reclamer(sessions))

    assert appels == [], "le patch d'une figure ne réindexe pas tout le document"
    with _session(sessions) as db:
        figure = db.get(ArtifactFigure, figure_id)
        representation = db.get(ArtifactRepresentation, representation_id)
        chunk_figure = db.get(ArtifactChunk, ids[0])
        chunk_autre = db.get(ArtifactChunk, ids[1])

        assert figure.status == figures_store.STATUT_DONE
        assert figure.description == description

        # La description est dans le fragment, une seule fois ; son identité,
        # son ordinal et sa révision de fiche sont conservés.
        assert description in chunk_figure.text
        assert chunk_figure.text.count(description) == 1
        assert chunk_figure.id == ids[0]
        assert chunk_figure.ordinal == 0
        assert chunk_figure.card_revision == 7
        assert chunk_figure.figures_revision == representation.figures_revision == 1
        assert chunk_figure.text_sha256 == chunks_module._sha256_texte(
            chunk_figure.text
        )
        assert attente not in chunk_figure.text

        # Le fragment voisin n'est pas touché.
        assert (chunk_autre.text, chunk_autre.text_sha256) == avant[ids[1]]

        # FTS : un terme présent SEULEMENT dans la description est retrouvé.
        lignes = db.execute(
            sql_text(
                "SELECT chunk_id FROM chunk_search"
                " WHERE chunk_search MATCH :q AND artifact_id = :a"
            ),
            {"q": "vermillon", "a": artifact_id},
        ).fetchall()
        assert [ligne[0] for ligne in lignes] == [ids[0]]

        # Vecteur du fragment modifié supprimé, celui du voisin conservé.
        assert (
            db.execute(
                sql_text(
                    "SELECT COUNT(*) FROM artifact_chunk_vectors WHERE chunk_id = :c"
                ),
                {"c": ids[0]},
            ).scalar()
            == 0
        )
        assert (
            db.execute(
                sql_text(
                    "SELECT COUNT(*) FROM artifact_chunk_vectors WHERE chunk_id = :c"
                ),
                {"c": ids[1]},
            ).scalar()
            == 1
        )

        # Aucune nouvelle révision de fiche, aucun enqueue mémoire.
        cartes = (
            db.query(ArtifactCard).filter(ArtifactCard.artifact_id == artifact_id).all()
        )
        assert len(cartes) == 1 and cartes[0].revision == 1
        assert cartes[0].summary == "résumé initial"
        assert (
            db.query(MemoryDocumentExtraction)
            .filter(MemoryDocumentExtraction.artifact_id == artifact_id)
            .count()
            == 0
        )


def test_placeholder_absent_marque_limite_et_laisse_le_texte_intact(monde, monkeypatch):
    sessions, racine = monde
    artifact_id, representation_id, figure_id = _creer_document(sessions, racine)
    ids = _poser_fragments(
        sessions, artifact_id, representation_id, ["FRAGMENT-SANS-PLACEHOLDER"]
    )
    description = "Une carte ancienne en couleur."
    monkeypatch.setattr(
        "backend.ingestion.vision_appel.appeler_vision_generale",
        lambda consigne, pixels: description,
    )

    _executer(_reclamer(sessions))

    with _session(sessions) as db:
        figure = db.get(ArtifactFigure, figure_id)
        representation = db.get(ArtifactRepresentation, representation_id)
        chunk = db.get(ArtifactChunk, ids[0])
        assert figure.status == figures_store.STATUT_LIMITE
        assert figure.last_error and "placeholder introuvable" in figure.last_error
        assert figure.description == description
        # La représentation reste enrichie : seule la recherche du fragment
        # est incomplète, et le texte n'a pas été corrompu.
        assert description in representation.markdown
        assert chunk.text == "FRAGMENT-SANS-PLACEHOLDER"
        assert chunk.text_sha256 == chunks_module._sha256_texte(chunk.text)
        assert (
            db.execute(
                sql_text(
                    "SELECT COUNT(*) FROM artifact_chunk_vectors WHERE chunk_id = :c"
                ),
                {"c": ids[0]},
            ).scalar()
            == 1
        )


def test_placeholder_ambigu_marque_limite_sans_toucher_aucun_fragment(
    monde, monkeypatch
):
    sessions, racine = monde
    artifact_id, representation_id, figure_id = _creer_document(sessions, racine)
    attente = figures_pdf.bloc_figure_en_attente(1, 1)
    # Vraie ambiguïté : DEUX occurrences dans UN MÊME fragment. Deux fragments
    # porteurs à une occurrence chacun sont le cas normal du recouvrement et
    # doivent être patchés ensemble (voir le test de frontière ci-dessous).
    textes = [f"FRAGMENT-UN\n\n{attente}\n\n{attente}", "FRAGMENT-DEUX"]
    ids = _poser_fragments(sessions, artifact_id, representation_id, textes)
    description = "Une photographie de bâtiment."
    monkeypatch.setattr(
        "backend.ingestion.vision_appel.appeler_vision_generale",
        lambda consigne, pixels: description,
    )

    _executer(_reclamer(sessions))

    with _session(sessions) as db:
        figure = db.get(ArtifactFigure, figure_id)
        assert figure.status == figures_store.STATUT_LIMITE
        assert figure.last_error and "répété" in figure.last_error
        assert "2 fois" in figure.last_error
        for chunk_id, texte in zip(ids, textes):
            chunk = db.get(ArtifactChunk, chunk_id)
            assert chunk.text == texte
            assert description not in chunk.text
            assert (
                db.execute(
                    sql_text(
                        "SELECT COUNT(*) FROM artifact_chunk_vectors"
                        " WHERE chunk_id = :c"
                    ),
                    {"c": chunk_id},
                ).scalar()
                == 1
            )


def test_reappliquer_la_meme_description_ne_duplique_pas(monde):
    sessions, racine = monde
    artifact_id, representation_id, figure_id = _creer_document(sessions, racine)
    attente = figures_pdf.bloc_figure_en_attente(1, 1)
    ids = _poser_fragments(
        sessions, artifact_id, representation_id, [f"AVANT\n{attente}\nAPRES"]
    )
    description = "Un schéma circulaire bleu."
    figure = {
        "figure_id": figure_id,
        "page": 1,
        "region": 1,
        "description": description,
    }

    with _session(sessions) as db:
        premier = chunks_module.mettre_a_jour_chunks_figures(
            db, artifact_id, representation_id, [figure], figures_revision=3
        )
        db.commit()
        second = chunks_module.mettre_a_jour_chunks_figures(
            db, artifact_id, representation_id, [figure], figures_revision=3
        )
        db.commit()

        assert premier.figures_appliquees == 1
        assert second.figures_appliquees == 0
        assert second.figures_deja_appliquees == 1
        assert second.figures_limite == ()

        chunk = db.get(ArtifactChunk, ids[0])
        assert chunk.text.count(description) == 1
        assert attente not in chunk.text
        assert (
            db.execute(
                sql_text("SELECT COUNT(*) FROM chunk_search WHERE chunk_id = :c"),
                {"c": ids[0]},
            ).scalar()
            == 1
        )


def test_sans_fragment_la_description_reste_appliquee_et_la_figure_done(
    monde, monkeypatch
):
    """Rien à patcher n'est pas une limite : l'indexation n'a pas encore eu lieu."""
    sessions, racine = monde
    _, _, figure_id = _creer_document(sessions, racine)
    monkeypatch.setattr(
        "backend.ingestion.vision_appel.appeler_vision_generale",
        lambda consigne, pixels: "Un diagramme sans fragment indexé.",
    )

    _executer(_reclamer(sessions))

    figure = _figure(sessions, figure_id)
    assert figure.status == figures_store.STATUT_DONE
    assert figure.last_error is None


def _texte_avec_placeholder_a_frontiere(attente: str) -> str:
    """Texte réel dont le recouvrement de `chunk_text` duplique `attente`.

    Vingt phrases (~22 tokens chacune) remplissent le premier fragment, puis le
    placeholder occupe la fin de ce fragment et le début du suivant grâce aux
    15 % de recouvrement. Les deux fragments porteurs contiennent alors
    exactement une occurrence du placeholder.
    """
    phrase = (
        "Phrase de remplissage stable et suffisamment longue pour peser "
        "dans le budget de tokens."
    )
    texte = "\n\n".join([" ".join([phrase] * 20), attente, " ".join([phrase] * 5)])
    porteurs = [
        index
        for index, chunk in enumerate(chunks_module.chunk_text(texte))
        if attente in chunk
    ]
    assert porteurs == [0, 1], porteurs
    return texte


def test_recouvrement_patche_les_deux_porteurs_et_reste_cherchable(monde, monkeypatch):
    """Le recouvrement du vrai chunker ne doit pas créer de fausse ambiguïté."""
    sessions, racine = monde
    artifact_id, representation_id, figure_id = _creer_document(sessions, racine)
    attente = figures_pdf.bloc_figure_en_attente(1, 1)
    texte = _texte_avec_placeholder_a_frontiere(attente)

    with _session(sessions) as db:
        art = db.get(Artifact, artifact_id)
        nombre = chunks_module.ensure_chunks_for_artifact(
            db,
            art,
            texte,
            [("workspace", "ws_frontiere")],
            representation_id=representation_id,
        )
        db.commit()
        chunks = (
            db.query(ArtifactChunk)
            .filter(ArtifactChunk.artifact_id == artifact_id)
            .order_by(ArtifactChunk.ordinal.asc())
            .all()
        )
        ids = [chunk.id for chunk in chunks]

    assert nombre == len(ids) == 2
    porteurs = [chunk for chunk in chunks if attente in chunk.text]
    assert len(porteurs) == 2, "le recouvrement doit produire deux porteurs"

    description = "Une zibeline compose un paysage de glace."
    monkeypatch.setattr(
        "backend.ingestion.vision_appel.appeler_vision_generale",
        lambda consigne, pixels: description,
    )

    _executer(_reclamer(sessions))

    with _session(sessions) as db:
        figure = db.get(ArtifactFigure, figure_id)
        assert figure.status == figures_store.STATUT_DONE
        assert figure.last_error is None
        for chunk_id in ids:
            chunk = db.get(ArtifactChunk, chunk_id)
            assert description in chunk.text
            assert chunk.text.count(description) == 1
            assert attente not in chunk.text
            assert chunk.text_sha256 == chunks_module._sha256_texte(chunk.text)
            assert (
                db.execute(
                    sql_text(
                        "SELECT COUNT(*) FROM artifact_chunk_vectors"
                        " WHERE chunk_id = :c"
                    ),
                    {"c": chunk_id},
                ).scalar()
                == 0
            )

        retrouves = db.execute(
            sql_text(
                "SELECT chunk_id FROM chunk_search"
                " WHERE chunk_search MATCH :q AND artifact_id = :a"
            ),
            {"q": "zibeline", "a": artifact_id},
        ).fetchall()
        assert sorted(ligne[0] for ligne in retrouves) == sorted(ids)


def test_index_legacy_avec_placeholder_est_patche_et_rattache(monde, monkeypatch):
    sessions, racine = monde
    artifact_id, representation_id, figure_id = _creer_document(sessions, racine)
    attente = figures_pdf.bloc_figure_en_attente(1, 1)
    ids = _poser_fragments(
        sessions, artifact_id, None, [f"LEGACY\n\n{attente}\n\nFIN-LEGACY"]
    )
    description = "Une hélice métallique décrite depuis un index legacy."
    monkeypatch.setattr(
        "backend.ingestion.vision_appel.appeler_vision_generale",
        lambda consigne, pixels: description,
    )

    _executer(_reclamer(sessions))

    with _session(sessions) as db:
        figure = db.get(ArtifactFigure, figure_id)
        chunk = db.get(ArtifactChunk, ids[0])
        assert figure.status == figures_store.STATUT_DONE
        assert figure.last_error is None
        assert description in chunk.text
        assert attente not in chunk.text
        assert chunk.representation_id == representation_id
        assert (
            db.execute(
                sql_text(
                    "SELECT COUNT(*) FROM artifact_chunk_vectors WHERE chunk_id = :c"
                ),
                {"c": ids[0]},
            ).scalar()
            == 0
        )
        retrouves = db.execute(
            sql_text(
                "SELECT chunk_id FROM chunk_search"
                " WHERE chunk_search MATCH :q AND chunk_id = :c",
            ),
            {"q": "legacy", "c": ids[0]},
        ).fetchall()
        assert len(retrouves) == 1
        row = db.execute(
            sql_text("SELECT text FROM chunk_search WHERE chunk_id = :c"),
            {"c": ids[0]},
        ).fetchone()
        assert "métallique" in row[0]


def test_legacy_sans_placeholder_est_un_echec_motive(monde, monkeypatch):
    sessions, racine = monde
    artifact_id, representation_id, figure_id = _creer_document(sessions, racine)
    ids = _poser_fragments(
        sessions, artifact_id, None, ["FRAGMENT-LEGACY-SANS-PLACEHOLDER"]
    )
    description = "Une description appliquée que le legacy ne porte pas."
    monkeypatch.setattr(
        "backend.ingestion.vision_appel.appeler_vision_generale",
        lambda consigne, pixels: description,
    )

    _executer(_reclamer(sessions))

    with _session(sessions) as db:
        figure = db.get(ArtifactFigure, figure_id)
        representation = db.get(ArtifactRepresentation, representation_id)
        chunk = db.get(ArtifactChunk, ids[0])
        assert figure.status == figures_store.STATUT_LIMITE
        assert figure.last_error and "figure hors index legacy" in figure.last_error
        assert "placeholder introuvable" not in figure.last_error
        assert description in representation.markdown
        assert chunk.text == "FRAGMENT-LEGACY-SANS-PLACEHOLDER"
        assert chunk.representation_id is None


def test_echec_sql_du_patch_laisse_la_description_et_marque_limite(monde, monkeypatch):
    """Un FTS qui échoue n'efface pas la description ni ne corrompt le fragment."""
    from sqlalchemy.exc import OperationalError

    sessions, racine = monde
    artifact_id, representation_id, figure_id = _creer_document(sessions, racine)
    attente = figures_pdf.bloc_figure_en_attente(1, 1)
    texte_initial = f"DEBUT\n\n{attente}\n\nFIN"
    ids = _poser_fragments(sessions, artifact_id, representation_id, [texte_initial])
    description = "Une sculpture de bronze visible malgré l'échec SQL."
    monkeypatch.setattr(
        "backend.ingestion.vision_appel.appeler_vision_generale",
        lambda consigne, pixels: description,
    )

    def _remplacement_impossible(*args, **kwargs):
        raise OperationalError(
            "UPDATE chunk_search", {}, Exception("disque plein (test)")
        )

    monkeypatch.setattr(
        chunks_module, "_remplacer_lignes_fts", _remplacement_impossible
    )

    _executer(_reclamer(sessions))

    with _session(sessions) as db:
        figure = db.get(ArtifactFigure, figure_id)
        representation = db.get(ArtifactRepresentation, representation_id)
        chunk = db.get(ArtifactChunk, ids[0])
        assert figure.status == figures_store.STATUT_LIMITE
        assert (
            figure.last_error and "patch des fragments impossible" in figure.last_error
        )
        assert "disque plein (test)" in figure.last_error
        # La description reste dans la représentation : le savepoint des
        # fragments n'annule pas le patch documentaire déjà appliqué.
        assert description in representation.markdown
        assert attente not in representation.markdown
        # Le fragment est intact : ni texte patché, ni empreinte différente.
        assert chunk.text == texte_initial
        assert chunk.text_sha256 == chunks_module._sha256_texte(texte_initial)
        assert (
            db.execute(
                sql_text(
                    "SELECT COUNT(*) FROM artifact_chunk_vectors WHERE chunk_id = :c"
                ),
                {"c": ids[0]},
            ).scalar()
            == 1
        )


def test_remplacer_lignes_fts_conserve_toutes_les_portees(monde):
    sessions, racine = monde
    artifact_id, _, _ = _creer_document(sessions, racine)
    chunk_id = f"chk_portees_{artifact_id}"
    portees = [
        ("workspace", "ws_frontiere"),
        ("conversation", "conv_42"),
        ("library", "lib_7"),
    ]
    with _session(sessions) as db:
        for scope_kind, scope_id in portees:
            db.execute(
                sql_text(
                    """INSERT INTO chunk_search(
                           chunk_id, artifact_id, scope_kind, scope_id, text, context)
                       VALUES (:c, :a, :sk, :sid, 'ancien texte', 'ancien contexte')"""
                ),
                {
                    "c": chunk_id,
                    "a": artifact_id,
                    "sk": scope_kind,
                    "sid": scope_id,
                },
            )
        db.commit()

        chunks_module._remplacer_lignes_fts(
            db, artifact_id, chunk_id, "texte neuf", "contexte neuf"
        )
        db.commit()

        lignes = db.execute(
            sql_text(
                "SELECT scope_kind, scope_id, text, context FROM chunk_search"
                " WHERE chunk_id = :c"
            ),
            {"c": chunk_id},
        ).fetchall()

    assert len(lignes) == len(portees)
    assert {(ligne[0], ligne[1]) for ligne in lignes} == set(portees)
    assert all(ligne[2] == "texte neuf" for ligne in lignes)
    assert all(ligne[3] == "contexte neuf" for ligne in lignes)


def test_remplacer_lignes_fts_repli_sur_les_liens_actifs(monde):
    sessions, racine = monde
    artifact_id, _, _ = _creer_document(sessions, racine)
    chunk_id = f"chk_sans_portee_{artifact_id}"
    with _session(sessions) as db:
        db.add_all(
            [
                ArtifactLink(
                    id=f"lnk_ws_{artifact_id}",
                    artifact_id=artifact_id,
                    scope_kind="workspace",
                    scope_id="ws_actif",
                ),
                ArtifactLink(
                    id=f"lnk_lib_{artifact_id}",
                    artifact_id=artifact_id,
                    scope_kind="library",
                    scope_id="lib_active",
                ),
                ArtifactLink(
                    id=f"lnk_old_{artifact_id}",
                    artifact_id=artifact_id,
                    scope_kind="conversation",
                    scope_id="conv_retiree",
                    removed_at=utc_naif(),
                ),
            ]
        )
        db.commit()

        chunks_module._remplacer_lignes_fts(
            db, artifact_id, chunk_id, "texte du repli", "contexte du repli"
        )
        db.commit()

        lignes = db.execute(
            sql_text(
                "SELECT scope_kind, scope_id FROM chunk_search WHERE chunk_id = :c"
            ),
            {"c": chunk_id},
        ).fetchall()

    assert {(ligne[0], ligne[1]) for ligne in lignes} == {
        ("workspace", "ws_actif"),
        ("library", "lib_active"),
    }


def test_decrire_figure_refuse_une_position_invalide():
    with pytest.raises(ValueError, match="page/région invalides"):
        chunks_module._decrire_figure(
            {"figure_id": "fig_x", "page": "sud", "region": 1, "description": "d"}
        )
    with pytest.raises(ValueError, match="hors domaine"):
        chunks_module._decrire_figure(
            {"figure_id": "fig_x", "page": 0, "region": 1, "description": "d"}
        )


# ---------------------------------------------------------------------------
# 9. Arrêt du worker : réveille le wait et joint le thread
# ---------------------------------------------------------------------------


def test_arret_du_worker_reveille_le_wait_et_joint_le_thread(monde):
    figures_worker.demarrer_worker_figures()
    fil = figures_worker._thread
    assert fil is not None and fil.is_alive()
    resultat = figures_worker.arreter_worker_figures()
    assert resultat["statut"] == "arrete"
    assert not fil.is_alive()
    assert figures_worker._thread is None
