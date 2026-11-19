"""Étape A de l'ingestion en deux étages — représentations et placeholders.

Ces tests verrouillent les deux gains de l'étape A :

- une représentation COMPLÈTE et versionnée (le texte n'est plus borné à 16 k) ;
- des figures REPÉRÉES mais non décrites : placeholders positionnels et lignes
  `artifact_figures` en attente, sans AUCUN appel LFM-VL.

Aucun modèle réel, aucun GPU : le layout et l'OCR sont des doubles. Les PDF sont
de vrais fichiers PyMuPDF, pour que le routage page/région soit exercé.
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, text as sql_text
from sqlalchemy.orm import sessionmaker

from backend.db import database as database_module
from backend.db import migrations as migrations_module
from backend.db.migrations import apply_migrations
from backend.db.models import (
    Artifact,
    ArtifactFigure,
    ArtifactRepresentation,
    Base,
    Blob,
    Observation,
)
from backend.ingestion import (
    attachment_ingest,
    bail,
    etat,
    figures_pdf,
    figures_store,
    representations,
)
from backend.ingestion.erreurs import BailPerdu
from backend.ipc import attachments as A
from backend.ipc import paths_bridge as paths_bridge_module

ECHELLE = 200 / 72  # rendu 200 ppp : points PDF → pixels


# ---------------------------------------------------------------------------
# Montages partagés
# ---------------------------------------------------------------------------


@contextmanager
def _session(sessions):
    db = sessions()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture()
def monde(tmp_path, monkeypatch):
    """Base neuve, index FTS, racines de données jetables, ingestion neutralisée."""
    moteur = create_engine(f"sqlite:///{tmp_path / 'representations.db'}")
    Base.metadata.create_all(moteur)
    from backend.db.database import _ensure_fts
    from backend.search.chunks import ensure_chunk_tables

    _ensure_fts(moteur)
    sessions = sessionmaker(bind=moteur, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(database_module, "SessionLocal", sessions)
    racine = tmp_path / "app"
    (racine / "blobs").mkdir(parents=True)
    (racine / "temp").mkdir(parents=True)
    monkeypatch.setenv("THOT_PORTABLE", "1")
    monkeypatch.setenv("THOT_APP_DATA_DIR", str(racine))
    monkeypatch.setattr(
        paths_bridge_module, "blobs_root", lambda: racine / "blobs", raising=False
    )
    monkeypatch.setattr(
        paths_bridge_module, "temp_root", lambda: racine / "temp", raising=False
    )
    monkeypatch.setattr(
        paths_bridge_module,
        "data_dirs",
        lambda: (racine, racine / "temp"),
        raising=False,
    )
    monkeypatch.setattr(A, "_declencher_ingestion", lambda ids: None, raising=False)
    with _session(sessions) as db:
        ensure_chunk_tables(db)
        db.commit()
    return sessions, racine


def _pdf_scanne_avec_figure(chemin) -> None:
    import pymupdf

    source = pymupdf.open()
    page = source.new_page(width=595, height=842)
    page.insert_text((50, 60), "TEXTE-DE-PAGE-CARTE-4", fontsize=14)
    page.draw_rect(pymupdf.Rect(120, 120, 320, 320), color=(0, 0, 0), fill=(1, 0, 0))
    pixmap = page.get_pixmap(dpi=150)
    cible = pymupdf.open()
    page_cible = cible.new_page(width=595, height=842)
    page_cible.insert_image(page_cible.rect, stream=pixmap.tobytes("png"))
    cible.save(str(chemin))
    source.close()
    cible.close()


def _pdf_natif_avec_figure(chemin) -> None:
    import pymupdf

    source = pymupdf.open()
    page_fig = source.new_page(width=200, height=200)
    page_fig.draw_rect(pymupdf.Rect(0, 0, 200, 200), color=(0, 0, 0), fill=(1, 0, 0))
    pixmap = page_fig.get_pixmap(dpi=100)

    document = pymupdf.open()
    page = document.new_page(width=595, height=842)
    page.insert_text((72, 100), "HAUT-DU-DOCUMENT " * 4, fontsize=14)
    page.insert_text((72, 700), "BAS-DU-DOCUMENT " * 4, fontsize=14)
    page.insert_image(pymupdf.Rect(120, 300, 320, 500), stream=pixmap.tobytes("png"))
    document.save(str(chemin))
    source.close()
    document.close()


def _pdf_natif_sans_figure(chemin, pages: int = 1) -> None:
    import pymupdf

    document = pymupdf.open()
    for index in range(pages):
        page = document.new_page()
        page.insert_text(
            (72, 110),
            f"PAGE-{index + 1:02d} contenu natif suffisamment long pour rester "
            "natif sans aucun modèle.",
            fontsize=14,
        )
    document.save(str(chemin))
    document.close()


def _pdf_mixte(chemin) -> None:
    """Page 1 native sans image, page 2 scannée (image sans couche texte)."""
    import pymupdf

    source = pymupdf.open()
    page_scan = source.new_page(width=595, height=842)
    page_scan.insert_text((72, 120), "PAGE-SCAN-OCR", fontsize=22)
    pixmap = page_scan.get_pixmap(dpi=150)

    document = pymupdf.open()
    page_native = document.new_page(width=595, height=842)
    page_native.insert_text((72, 100), "PAGE-NATIVE-TEXTE " * 5, fontsize=14)
    page_image = document.new_page(width=595, height=842)
    page_image.insert_image(page_image.rect, stream=pixmap.tobytes("png"))
    document.save(str(chemin))
    source.close()
    document.close()


def _deposer(sessions, octets: bytes, nom: str) -> str:
    conversation = f"conv_{uuid.uuid4().hex[:12]}"
    with _session(sessions) as db:
        db.execute(
            sql_text(
                "INSERT INTO conversations (id,title,mode,created_at,updated_at)"
                " VALUES (:c,'representations','chat',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            ),
            {"c": conversation},
        )
        db.commit()
        item = A.stage_picker_bytes(
            db, {"requestId": "representations", "filename": nom, "bytes": octets}
        )["items"][0]
        (artefact,) = A.attachments_prepare(
            db,
            {
                "conversationId": conversation,
                "stageItemIds": [item["stage_item_id"]],
                "names": [nom],
            },
        )["artifact_ids"]
    return artefact


def _artifact(sessions, artefact: str) -> Artifact:
    with _session(sessions) as db:
        return db.query(Artifact).filter(Artifact.id == artefact).one()


def _representation_courante(sessions, artefact: str):
    with _session(sessions) as db:
        return representations.representation_pour(db, artefact)


def _figures(sessions, artefact: str) -> list[ArtifactFigure]:
    with _session(sessions) as db:
        return (
            db.query(ArtifactFigure)
            .filter(ArtifactFigure.artifact_id == artefact)
            .order_by(ArtifactFigure.page, ArtifactFigure.region)
            .all()
        )


def _observation(sessions, artefact: str) -> Observation:
    with _session(sessions) as db:
        lignes = (
            db.query(Observation)
            .filter(
                Observation.artifact_id == artefact,
                Observation.tool_name == "attachment_ingest",
            )
            .all()
        )
        assert lignes
        return lignes[-1]


# ---------------------------------------------------------------------------
# 1. Migration : base neuve, base existante, réapplication idempotente
# ---------------------------------------------------------------------------


def _tables(engine) -> set[str]:
    with engine.connect() as conn:
        return {
            str(row[0])
            for row in conn.execute(
                sql_text("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        }


def _colonnes(engine, table: str) -> set[str]:
    with engine.connect() as conn:
        return {
            str(row[1])
            for row in conn.execute(sql_text(f"PRAGMA table_info({table})")).fetchall()
        }


def _index(engine, table: str) -> set[str]:
    with engine.connect() as conn:
        return {
            str(row[1])
            for row in conn.execute(sql_text(f"PRAGMA index_list({table})")).fetchall()
        }


def test_migration_cree_tables_et_colonnes_sur_base_neuve(tmp_path):
    moteur = create_engine(f"sqlite:///{tmp_path / 'neuve.db'}")
    Base.metadata.create_all(moteur)
    apply_migrations(moteur)

    tables = _tables(moteur)
    assert {"artifact_representations", "artifact_figures"} <= tables
    assert {
        "figures_state",
        "figures_done",
        "figures_total",
        "figures_limited",
        "figures_reason",
        "figures_updated_at",
    } <= _colonnes(moteur, "artifacts")
    assert {
        "representation_id",
        "figures_revision",
        "text_sha256",
    } <= _colonnes(moteur, "artifact_chunks")
    assert "uq_representations_current" in _index(moteur, "artifact_representations")
    assert "idx_representations_artifact_revision" in _index(
        moteur, "artifact_representations"
    )
    assert "idx_figures_status_next" in _index(moteur, "artifact_figures")
    assert "idx_figures_artifact_status" in _index(moteur, "artifact_figures")
    assert "idx_figures_crop_sha256" in _index(moteur, "artifact_figures")
    # `create_all` a produit le même schéma : pas de table manquante.
    assert {
        "artifact_representations",
        "artifact_figures",
    } <= set(Base.metadata.tables)


def test_migration_additive_sur_base_existante_et_idempotente(tmp_path):
    """Base « ancienne » : tables socle minimales, versions 1..21 déjà appliquées."""
    moteur = create_engine(f"sqlite:///{tmp_path / 'ancienne.db'}")
    with moteur.begin() as conn:
        conn.execute(
            sql_text(
                "CREATE TABLE schema_migrations ("
                "version INTEGER PRIMARY KEY, name VARCHAR NOT NULL, "
                "applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
        )
        for version in range(1, 22):
            conn.execute(
                sql_text(
                    "INSERT INTO schema_migrations(version, name) VALUES (:v, 'x')"
                ),
                {"v": version},
            )
        conn.execute(
            sql_text(
                "CREATE TABLE artifacts ("
                "id VARCHAR PRIMARY KEY, blob_id VARCHAR NOT NULL, kind VARCHAR NOT NULL, "
                "original_filename VARCHAR NOT NULL, display_name VARCHAR NOT NULL, "
                "ingest_state VARCHAR NOT NULL DEFAULT 'inconnu')"
            )
        )
        conn.execute(
            sql_text(
                "CREATE TABLE artifact_chunks ("
                "id VARCHAR PRIMARY KEY, artifact_id VARCHAR NOT NULL, "
                "card_revision INTEGER, ordinal INTEGER, text TEXT NOT NULL, "
                "context TEXT, created_at DATETIME)"
            )
        )

    apply_migrations(moteur)
    assert "artifact_representations" in _tables(moteur)
    assert "artifact_figures" in _tables(moteur)
    assert "figures_state" in _colonnes(moteur, "artifacts")
    assert "representation_id" in _colonnes(moteur, "artifact_chunks")

    # Réapplication DIRECTE de la migration 22 : les DDL sont tous IF NOT EXISTS
    # et `_add_column` est idempotent. Un second passage ne doit rien lever.
    with moteur.begin() as conn:
        migrations_module._migrate_representations_et_figures(conn)
        migrations_module._migrate_representations_et_figures(conn)
    assert "figures_state" in _colonnes(moteur, "artifacts")
    assert "uq_representations_current" in _index(moteur, "artifact_representations")


# ---------------------------------------------------------------------------
# 2. Représentation : Markdown complet, compteurs, version courante unique
# ---------------------------------------------------------------------------


def _artifact_pour_representation(sessions, tmp_path) -> str:
    artifact_id = f"art_{uuid.uuid4().hex[:12]}"
    blob_id = f"blob_{uuid.uuid4().hex[:12]}"
    with _session(sessions) as db:
        db.add(
            Blob(
                id=blob_id,
                sha256=uuid.uuid4().hex,
                relative_path=f"blobs/{blob_id}.bin",
                size_bytes=1,
                mime_detected="text/plain",
            )
        )
        db.add(
            Artifact(
                id=artifact_id,
                blob_id=blob_id,
                kind="text",
                original_filename="long.txt",
                display_name="long.txt",
                ingest_state=etat.ETAT_EXTRAIT,
            )
        )
        db.commit()
    return artifact_id


def test_representation_stocke_le_markdown_complet_et_perime_l_ancienne(
    monde, tmp_path
):
    sessions, _ = monde
    artefact = _artifact_pour_representation(sessions, tmp_path)
    texte = "A" * 100_000
    with _session(sessions) as db:
        premiere = representations.creer_representation(
            db,
            artefact,
            texte,
            [
                {
                    "page": 1,
                    "region": None,
                    "type": "texte",
                    "bbox": None,
                    "ordre": 0,
                    "contenu": texte,
                }
            ],
            extractor_version="test/v1",
            commit=True,
        )
        assert premiere.chars_total == 100_000
        assert len(premiere.markdown) == 100_000
        assert premiere.markdown_sha256 == representations.sha256_texte(texte)
        assert premiere.is_current == 1
        blocs = json.loads(premiere.blocks_json)
        assert blocs[0]["page"] == 1
        assert blocs[0]["contenu"] == texte
        assert set(blocs[0]) == {
            "page",
            "region",
            "type",
            "bbox",
            "ordre",
            "contenu",
        }

        seconde = representations.creer_representation(
            db,
            artefact,
            "B" * 12,
            None,
            extractor_version="test/v2",
            commit=True,
        )
        assert seconde.revision == premiere.revision + 1

        courante = representations.representation_pour(db, artefact)
        assert courante is not None and courante.id == seconde.id
        ancienne = (
            db.query(ArtifactRepresentation)
            .filter(ArtifactRepresentation.id == premiere.id)
            .one()
        )
        assert ancienne.is_current == 0
        assert ancienne.superseded_at is not None
        assert (
            db.query(ArtifactRepresentation)
            .filter(
                ArtifactRepresentation.artifact_id == artefact,
                ArtifactRepresentation.is_current == 1,
            )
            .count()
            == 1
        )

        # Bascule explicite : la première redevient courante, la seconde périmée.
        representations.basculer_courante(db, artefact, premiere.id, commit=True)
        assert representations.representation_pour(db, artefact).id == premiere.id


# ---------------------------------------------------------------------------
# 3. Aucun appel LFM-VL pendant l'étape A
# ---------------------------------------------------------------------------


def test_aucun_appel_vlm_pendant_l_extraction_pdf(monkeypatch, tmp_path):
    chemin = tmp_path / "scan.pdf"
    _pdf_scanne_avec_figure(chemin)
    monkeypatch.setattr(
        "backend.ingestion.paddle_client.detect_regions",
        lambda png, **k: [
            {"label": "text", "score": 0.9, "box": [0, 0, 595 * ECHELLE, 60 * ECHELLE]},
            {"label": "figure", "score": 0.9, "box": [340, 340, 900, 900]},
        ],
    )
    monkeypatch.setattr(
        "backend.ingestion.paddle_client.parse_png",
        lambda png, *, task="parse", **k: "TEXTE-OCR",
    )
    monkeypatch.setattr(
        figures_pdf,
        "reexaminer_figure",
        lambda *a, **k: pytest.fail("étape A : `reexaminer_figure` interdit"),
    )
    monkeypatch.setattr(
        "backend.ingestion.vision_appel.appeler_vision_generale",
        lambda *a, **k: pytest.fail("étape A : vision interdite"),
    )

    texte, mode = attachment_ingest.extraire_texte("art", "pdf", str(chemin))
    assert mode == "ocr"
    assert "TEXTE-OCR" in texte
    assert "description en attente" in texte


# ---------------------------------------------------------------------------
# 4. Placeholder positionnel, à la bonne position de lecture
# ---------------------------------------------------------------------------


def test_placeholder_positionnel_entre_les_blocs_de_texte(monkeypatch, tmp_path):
    chemin = tmp_path / "natif-figure.pdf"
    _pdf_natif_avec_figure(chemin)
    boite = [
        round(120 * ECHELLE, 1),
        round(300 * ECHELLE, 1),
        round(320 * ECHELLE, 1),
        round(500 * ECHELLE, 1),
    ]
    monkeypatch.setattr(
        "backend.ingestion.paddle_client.detect_regions",
        lambda png, **k: [{"label": "figure", "score": 0.9, "box": boite}],
    )
    monkeypatch.setattr(
        "backend.ingestion.paddle_client.parse_png",
        lambda *a, **k: pytest.fail("le texte natif ne doit pas être OCRisé"),
    )

    markdown, _ = attachment_ingest.extraire_texte("art", "pdf", str(chemin))
    assert "<!-- thot:figure page=1 région=1 attente -->" in markdown
    assert "**Figure** (page 1 · région 1) : description en attente" in markdown
    assert markdown.index("HAUT-DU-DOCUMENT") < markdown.index("**Figure**")
    assert markdown.index("**Figure**") < markdown.index("BAS-DU-DOCUMENT")


# ---------------------------------------------------------------------------
# 5. Lignes `artifact_figures`, borne 8/page et compteurs
# ---------------------------------------------------------------------------


def test_figures_pending_bornees_et_compteurs(monde, tmp_path, monkeypatch):
    sessions, _ = monde
    chemin = tmp_path / "dix-figures.pdf"
    _pdf_scanne_avec_figure(chemin)
    regions = [
        {
            "label": "figure",
            "score": 0.9,
            "box": [10 + i * 50, 10 + i * 50, 90 + i * 50, 90 + i * 50],
        }
        for i in range(10)
    ]
    monkeypatch.setattr(
        "backend.ingestion.paddle_client.detect_regions",
        lambda png, **k: regions,
    )
    monkeypatch.setattr(
        "backend.ingestion.vision_appel.appeler_vision_generale",
        lambda *a, **k: pytest.fail("étape A : aucun appel VLM"),
    )
    monkeypatch.setattr(
        figures_pdf,
        "reexaminer_figure",
        lambda *a, **k: pytest.fail("étape A : aucun réexamen"),
    )

    artefact = _deposer(sessions, chemin.read_bytes(), "dix-figures.pdf")
    attachment_ingest._ingérer_un(artefact)

    figures = _figures(sessions, artefact)
    assert len(figures) == figures_pdf.CROPS_FIGURE_MAX_PAR_PAGE
    assert [f.region for f in figures] == list(
        range(1, figures_pdf.CROPS_FIGURE_MAX_PAR_PAGE + 1)
    )
    for figure in figures:
        assert figure.status == "pending"
        assert figure.page == 1
        assert figure.label == "figure"
        assert figure.score == pytest.approx(0.9)
        assert json.loads(figure.bbox_json) == [
            float(10 + (figure.region - 1) * 50),
            float(10 + (figure.region - 1) * 50),
            float(90 + (figure.region - 1) * 50),
            float(90 + (figure.region - 1) * 50),
        ]
        assert figure.crop_relpath is None, (
            "étape A : Stage B recalcule le crop depuis le PDF"
        )
        assert figure.extractor_version == attachment_ingest.VERSION_EXTRACTEUR

    lu = _artifact(sessions, artefact)
    assert lu.figures_state == "en_attente"
    assert lu.figures_total == 8
    assert lu.figures_done == 0
    assert lu.figures_limited == 2
    assert lu.figures_reason and "2 région" in lu.figures_reason
    assert lu.figures_updated_at is not None

    representation = _representation_courante(sessions, artefact)
    assert representation is not None
    assert representation.markdown.count("description en attente") == 8


def test_figures_limited_structure_survit_a_une_reformulation(monde):
    """Le compte de l'étape A ne dépend plus de la phrase française exacte."""
    sessions, _ = monde
    marque = uuid.uuid4().hex[:10]
    artifact_id = f"art_{marque}"

    with _session(sessions) as db:
        db.add(
            Blob(
                id=f"blob_{marque}",
                sha256=marque,
                relative_path=f"blobs/{marque}.pdf",
                size_bytes=1,
                mime_detected="application/pdf",
            )
        )
        db.add(
            Artifact(
                id=artifact_id,
                blob_id=f"blob_{marque}",
                kind="pdf",
                original_filename="raison.pdf",
                display_name="raison.pdf",
                ingest_state="extrait",
            )
        )
        db.commit()
        representation = representations.creer_representation(
            db,
            artifact_id,
            "TEXTE",
            [],
            extractor_version="test/raison",
            commit=False,
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
                    bbox=(1.0, 1.0, 10.0, 10.0),
                )
            ],
            extractor_version="test/raison",
        )
        figures_store.mettre_a_jour_etat_figures(db, artifact_id, figures_limited=2)
        db.commit()

    lu = _artifact(sessions, artifact_id)
    assert lu.figures_limited == 2
    assert json.loads(lu.figures_reason)["stage_a_limited"] == 2
    assert "2 région" in lu.figures_reason

    # Reformulation : la phrase change, le compte structuré reste la vérité.
    with _session(sessions) as db:
        db.query(Artifact).filter(Artifact.id == artifact_id).update(
            {
                "figures_reason": json.dumps(
                    {
                        "stage_a_limited": 2,
                        "reasons": ["borne reformulée autrement"],
                    },
                    ensure_ascii=False,
                )
            }
        )
        db.commit()
        figures_store.recalculer_etat_figures(db, artifact_id, commit=True)

    lu = _artifact(sessions, artifact_id)
    assert lu.figures_limited == 2
    assert json.loads(lu.figures_reason)["stage_a_limited"] == 2
    assert "2 région" in lu.figures_reason

    # Compatibilité : une raison LEGACY en phrase seule reste relue.
    assert (
        figures_store.regions_ecartees_depuis_raison(
            "2 région(s) figure(s) écartée(s) par la borne 8 par page"
        )
        == 2
    )


# ---------------------------------------------------------------------------
# 6. PDF natif avec figures : texte natif, placeholders, ni OCR ni VLM
# ---------------------------------------------------------------------------


def test_pdf_natif_avec_figures_ni_ocr_ni_vlm(monde, tmp_path, monkeypatch):
    sessions, _ = monde
    chemin = tmp_path / "natif-figure.pdf"
    _pdf_natif_avec_figure(chemin)
    boite = [
        round(120 * ECHELLE, 1),
        round(300 * ECHELLE, 1),
        round(320 * ECHELLE, 1),
        round(500 * ECHELLE, 1),
    ]
    monkeypatch.setattr(
        "backend.ingestion.paddle_client.detect_regions",
        lambda png, **k: [{"label": "figure", "score": 0.9, "box": boite}],
    )
    monkeypatch.setattr(
        "backend.ingestion.paddle_client.parse_png",
        lambda *a, **k: pytest.fail("aucune OCR de page pour un PDF natif"),
    )
    monkeypatch.setattr(
        "backend.ingestion.vision_appel.appeler_vision_generale",
        lambda *a, **k: pytest.fail("étape A : aucun VLM"),
    )

    artefact = _deposer(sessions, chemin.read_bytes(), "natif-figure.pdf")
    attachment_ingest._ingérer_un(artefact)

    representation = _representation_courante(sessions, artefact)
    assert representation is not None
    assert "HAUT-DU-DOCUMENT" in representation.markdown
    assert "BAS-DU-DOCUMENT" in representation.markdown
    assert "<!-- thot:figure page=1 région=1 attente -->" in representation.markdown
    assert len(_figures(sessions, artefact)) == 1

    lu = _artifact(sessions, artefact)
    assert lu.figures_state == "en_attente"
    assert lu.figures_total == 1


# ---------------------------------------------------------------------------
# 7. PDF sans figure : `sans`, aucune ligne
# ---------------------------------------------------------------------------


def test_pdf_sans_figure_etat_sans_et_aucune_ligne(monde, tmp_path, monkeypatch):
    sessions, _ = monde
    chemin = tmp_path / "sans-figure.pdf"
    _pdf_natif_sans_figure(chemin)
    monkeypatch.setattr(
        "backend.ingestion.paddle_client.detect_regions",
        lambda *a, **k: pytest.fail("un PDF natif sans image ne demande aucun layout"),
    )

    artefact = _deposer(sessions, chemin.read_bytes(), "sans-figure.pdf")
    attachment_ingest._ingérer_un(artefact)

    assert _figures(sessions, artefact) == []
    lu = _artifact(sessions, artefact)
    assert lu.figures_state == "sans"
    assert lu.figures_total == 0
    assert lu.figures_done == 0
    assert lu.figures_limited == 0
    assert _representation_courante(sessions, artefact) is not None


# ---------------------------------------------------------------------------
# 8. PDF scanné : OCR texte + placeholders, pas de double description
# ---------------------------------------------------------------------------


def test_pdf_scanne_ocr_texte_et_placeholder_sans_double_description(
    monde, tmp_path, monkeypatch
):
    sessions, _ = monde
    chemin = tmp_path / "scan.pdf"
    _pdf_scanne_avec_figure(chemin)
    appels_ocr: list[str] = []

    def _parse(png, *, task="parse", **k):
        appels_ocr.append(task)
        return "TEXTE-OCR-DE-PAGE"

    monkeypatch.setattr(
        "backend.ingestion.paddle_client.detect_regions",
        lambda png, **k: [
            {"label": "text", "score": 0.9, "box": [0, 0, 595 * ECHELLE, 60 * ECHELLE]},
            {"label": "figure", "score": 0.9, "box": [340, 340, 900, 900]},
        ],
    )
    monkeypatch.setattr("backend.ingestion.paddle_client.parse_png", _parse)
    monkeypatch.setattr(
        "backend.ingestion.vision_appel.appeler_vision_generale",
        lambda *a, **k: pytest.fail("étape A : aucun VLM"),
    )

    artefact = _deposer(sessions, chemin.read_bytes(), "scan.pdf")
    attachment_ingest._ingérer_un(artefact)

    representation = _representation_courante(sessions, artefact)
    assert representation is not None
    assert "TEXTE-OCR-DE-PAGE" in representation.markdown
    assert "<!-- thot:figure page=1 région=2 attente -->" in representation.markdown
    assert appels_ocr == ["parse"], (
        "seule la région texte part à l'OCR ; la figure n'est ni OCRisée ni décrite"
    )
    assert len(_figures(sessions, artefact)) == 1


# ---------------------------------------------------------------------------
# 9. Non-régression : routage mixte, troncature, représentation complète
# ---------------------------------------------------------------------------


def test_routage_mixte_et_troncature_conserves(monde, tmp_path, monkeypatch):
    sessions, _ = monde
    mixte = tmp_path / "mixte.pdf"
    _pdf_mixte(mixte)

    def _detect(png, **k):
        return [{"label": "text", "score": 0.9, "box": [0, 0, 300, 80]}]

    monkeypatch.setattr("backend.ingestion.paddle_client.detect_regions", _detect)
    monkeypatch.setattr(
        "backend.ingestion.paddle_client.parse_png",
        lambda png, *, task="parse", **k: "PAGE-SCAN-OCR reconnu",
    )

    artefact = _deposer(sessions, mixte.read_bytes(), "mixte.pdf")
    attachment_ingest._ingérer_un(artefact)
    representation = _representation_courante(sessions, artefact)
    assert representation is not None
    assert "PAGE-NATIVE-TEXTE" in representation.markdown
    assert "PAGE-SCAN-OCR" in representation.markdown
    assert "chaine=mixte" in representation.markdown

    # Troncature : 25 pages natives → 20 traitées, `tronque` conservé et la
    # représentation porte la couverture réelle (pages_restantes=5).
    long_pdf = tmp_path / "long.pdf"
    _pdf_natif_sans_figure(long_pdf, pages=25)
    artefact_long = _deposer(sessions, long_pdf.read_bytes(), "long.pdf")
    attachment_ingest._ingérer_un(artefact_long)

    lu = _artifact(sessions, artefact_long)
    assert lu.ingest_state == etat.ETAT_TRONQUE
    representation_longue = _representation_courante(sessions, artefact_long)
    assert representation_longue is not None
    assert "pages_restantes=5" in representation_longue.markdown
    assert "## Page 20" in representation_longue.markdown
    assert "## Page 21" not in representation_longue.markdown
    assert representation_longue.chars_total == len(representation_longue.markdown)


# ---------------------------------------------------------------------------
# 10. Ré-ingestion : les figures de l'ancienne version sont superseded
# ---------------------------------------------------------------------------


def test_reingestion_perime_les_figures_de_l_ancienne_representation(
    monde, tmp_path, monkeypatch
):
    """Une ré-ingestion ne laisse pas les anciennes figures en `pending`.

    `figures_state` et `figures_total` ne décrivent que la représentation
    COURANTE : les figures de la version périmée passent `superseded` dans la
    même transaction que le basculement, et l'étape B ne les voit plus.
    """
    sessions, _ = monde
    chemin = tmp_path / "natif-figure.pdf"
    _pdf_natif_avec_figure(chemin)
    boite = [
        round(120 * ECHELLE, 1),
        round(300 * ECHELLE, 1),
        round(320 * ECHELLE, 1),
        round(500 * ECHELLE, 1),
    ]
    monkeypatch.setattr(
        "backend.ingestion.paddle_client.detect_regions",
        lambda png, **k: [{"label": "figure", "score": 0.9, "box": boite}],
    )
    monkeypatch.setattr(
        "backend.ingestion.paddle_client.parse_png",
        lambda *a, **k: pytest.fail("le texte natif ne doit pas être OCRisé"),
    )

    artefact = _deposer(sessions, chemin.read_bytes(), "natif-figure.pdf")
    attachment_ingest._ingérer_un(artefact)

    ancienne = _representation_courante(sessions, artefact)
    assert ancienne is not None
    premieres = _figures(sessions, artefact)
    assert len(premieres) == 1
    assert premieres[0].status == figures_store.STATUT_PENDING

    attachment_ingest._ingérer_un(artefact)

    courante = _representation_courante(sessions, artefact)
    assert courante is not None and courante.id != ancienne.id
    with _session(sessions) as db:
        assert db.get(ArtifactRepresentation, ancienne.id).is_current == 0

    figures = _figures(sessions, artefact)
    anciennes = [f for f in figures if f.representation_id == ancienne.id]
    nouvelles = [f for f in figures if f.representation_id == courante.id]
    assert len(anciennes) == 1 and len(nouvelles) == 1
    assert all(f.status == figures_store.STATUT_SUPERSEDED for f in anciennes)
    assert all(f.status == figures_store.STATUT_PENDING for f in nouvelles)

    lu = _artifact(sessions, artefact)
    assert lu.figures_state == figures_store.ETAT_FIGURES_EN_ATTENTE
    assert lu.figures_total == len(nouvelles)

    with _session(sessions) as db:
        courantes = figures_store.figures_de_la_representation_courante(
            db, artefact
        ).all()
    assert [f.id for f in courantes] == [f.id for f in nouvelles]


# ---------------------------------------------------------------------------
# 11. Fencing de la persistance : token périmé → aucune écriture
# ---------------------------------------------------------------------------


def test_persister_extraction_fence_par_claim_token(monde, tmp_path):
    """La persistance n'écrit que si l'UPDATE conditionnel trouve le token.

    Le token périmé est testé APRÈS libération : la fenêtre SELECT → écriture
    qui existait avant est fermée, et aucune représentation ni figure ne reste.
    Le token valide, lui, écrit représentation, figure et compteurs.
    """
    sessions, _ = monde
    artefact = _artifact_pour_representation(sessions, tmp_path)
    collecte = attachment_ingest.CollecteExtraction(
        figures=[
            figures_pdf.FigurePlaceholder(
                page=1,
                region=1,
                label="figure",
                score=0.9,
                bbox=(0.0, 0.0, 10.0, 10.0),
            )
        ]
    )

    with _session(sessions) as db:
        token_perime = bail.acquérir_bail(db, artefact, "fence-perime")
    assert token_perime is not None
    with _session(sessions) as db:
        assert bail.libérer_bail(db, artefact, token_perime) is True

    with pytest.raises(BailPerdu):
        attachment_ingest._persister_extraction_complete(
            artefact, "texte complet", collecte, claim_token=token_perime
        )

    with _session(sessions) as db:
        assert (
            db.query(ArtifactRepresentation)
            .filter(ArtifactRepresentation.artifact_id == artefact)
            .count()
            == 0
        )
        assert (
            db.query(ArtifactFigure)
            .filter(ArtifactFigure.artifact_id == artefact)
            .count()
            == 0
        )

    with _session(sessions) as db:
        token_valide = bail.acquérir_bail(db, artefact, "fence-valide")
    assert token_valide is not None
    representation = attachment_ingest._persister_extraction_complete(
        artefact, "texte complet", collecte, claim_token=token_valide
    )
    assert representation is not None

    with _session(sessions) as db:
        assert (
            db.query(ArtifactRepresentation)
            .filter(ArtifactRepresentation.artifact_id == artefact)
            .count()
            == 1
        )
        figure = (
            db.query(ArtifactFigure)
            .filter(ArtifactFigure.artifact_id == artefact)
            .one()
        )
        assert figure.representation_id == representation.id
        assert figure.status == figures_store.STATUT_PENDING
        lu = db.query(Artifact).filter(Artifact.id == artefact).one()
        assert lu.figures_state == figures_store.ETAT_FIGURES_EN_ATTENTE
        assert lu.figures_total == 1
        assert bail.libérer_bail(db, artefact, token_valide) is True
