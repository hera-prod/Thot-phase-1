"""Une page en panne livre un résultat PARTIEL nommé, jamais un document abandonné.

LE DÉFAUT VERROUILLÉ ICI : la première exception de chaîne modèle — layout non
chargé (cause conservée par `paddle_worker._charger_tout`), OCR KO, worker
crashé — sortait de `backend.ingestion.attachment_ingest._ocr_pdf` tout droit
avec son `RuntimeError` : une page 1 en panne tuait la page 2, parfaitement
saine, ainsi que toutes les suivantes. Une seule page en panne doublait pour un
PDF entier en `echec`, à l'inverse de la promesse du worker lui-même
(`paddle_worker.py` : le layout est un complément, la cause est NOMMÉE, jamais
un contournement silencieux) et de la fiche :

  - `mecanismes/ingestion/pdf.md` §5.1 : « Échec d'une page → Résultat partiel,
    autres pages conservées » ;
  - `pdf.md` §4.6 : « Les résultats aboutis restent utilisables avec leurs
    limites » — le fail loud et le résultat partiel ne se contredisent pas :
    la page manquante porte une mention honnête, le reste du document reste.

Ce que chaque test atteste :

  - la page en panne livre sa mention nommée `(page N non reconnue — cause)`,
    rendue via `markdown_pdf.PageExtraite` — elle garde son rang physique ;
  - les pages restantes SONT extraites : un document qui a la moitié saine n'a
    pas la moitié de son contenu ;
  - l'ÉTAT d'ingestion reflète le partiel (raison « traitement partiel — … »)
    tout en restant dans le vocabulaire prescrit de `etat.py` — un partiel
    avec texte reste `extrait`/`tronqué`, pas un `echec`.

Montage : base neuve et jetable, `SessionLocal` redirigée, racines de données
jetables, chaîne modèle moquée page par page. Aucun modèle, aucun GPU, aucun
réseau — même outillage que `test_depot_texte.py`.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, text as sql_text
from sqlalchemy.orm import sessionmaker

from backend.db import database as database_module
from backend.db.models import Artifact, Base, Observation
from backend.ingestion import attachment_ingest, etat
from backend.ipc import attachments as A
from backend.ipc import paths_bridge as paths_bridge_module

PDF_DEUX_PAGES = None  # fabrique au test (`fitz` importé là où il est requis)


@contextmanager
def _session(sessions):
    db = sessions()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture()
def monde(tmp_path, monkeypatch):
    """Base neuve, index FTS et fragments, racines de données jetables.

    Le déclencheur d'ingestion est neutralisé : `_ingérer_un` est piloté à la
    main, en synchronisé, pour que la panne simulée parle du CODE et pas d'un
    fil de course.
    """
    moteur = create_engine(f"sqlite:///{tmp_path / 'page-en-panne.db'}")
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
    return sessions


def pdf_scanne_deux_pages(tmp_path) -> bytes:
    """Un PDF scanné DEUX PAGES : aucune couche texte, la chaîne OCR est due."""
    fitz = pytest.importorskip("fitz")
    document = fitz.open()
    document.new_page()  # page 1 : blanche, sans texte natif
    document.new_page()  # page 2 : blanche, sans texte natif
    destination = tmp_path / "scan.pdf"
    document.save(str(destination))
    document.close()
    return destination.read_bytes()


def _deposer(sessions, octets: bytes, nom: str) -> str:
    """Dépose par le VRAI chemin de dépôt. Renvoie l'identifiant d'artefact."""
    conversation = f"conv_{uuid.uuid4().hex[:12]}"
    with _session(sessions) as db:
        db.execute(
            sql_text(
                "INSERT INTO conversations (id,title,mode,created_at,updated_at)"
                " VALUES (:c,'page-en-panne','chat',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            ),
            {"c": conversation},
        )
        db.commit()
        item = A.stage_picker_bytes(
            db, {"requestId": "page-en-panne", "filename": nom, "bytes": octets}
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
        assert lignes, (
            "aucune observation d'ingestion : le document ne serait pas livré"
        )
        return lignes[-1]


def test_la_page_en_panne_dit_sa_cause_et_les_autres_pages_restent(
    monde, tmp_path, monkeypatch
):
    """Page 1 en panne, page 2 saine : mention honnête + page 2 extraite + PARTIEL nommé.

    Le simulateur fait PRECISEMENT le manque du staff : le travail de la page 1
    lève au point d'entrée instrumenté (`detect_regions`, bail OCR), exactement
    comme si le layout n'avait jamais chargé — alors que la page 2 suit sa
    chaîne normale et livre son texte.
    """
    appels = {"layout": 0}

    def _detect(png: bytes, **k):
        appels["layout"] += 1
        if appels["layout"] == 1:
            raise RuntimeError("PP-DocLayoutV3 indisponible : worker arrêté (test)")
        return [{"label": "text", "score": 0.9, "box": [0, 0, 100, 40]}]

    def _parse(png: bytes, *, task="parse", **k):
        return "RAPPORT-DE-PAGE-DEUX le contenu de la seconde est reconnu."

    monkeypatch.setattr("backend.ingestion.paddle_client.detect_regions", _detect)
    monkeypatch.setattr("backend.ingestion.paddle_client.parse_png", _parse)

    artefact = _deposer(monde, pdf_scanne_deux_pages(tmp_path), "scan.pdf")
    attachment_ingest._ingérer_un(artefact)

    observation = _observation(monde, artefact)
    contenu = str(observation.payload_ref or "")
    # La cause de la page 1 est nommée, sous SA rubrique physique (« Page 1 ») :
    assert "(page 1 non reconnue — RuntimeError: PP-DocLayoutV3 indisponible" in contenu
    assert "## Page 1" in contenu
    # La page 2 n'est pas mort-noyée par la panne de sa voisine :
    assert "## Page 2" in contenu
    assert "RAPPORT-DE-PAGE-DEUX" in contenu
    assert appels["layout"] == 2, (
        "une page en panne ne doit PAS interrompre la chaîne des pages "
        "restantes : la page 2 a droit à sa détection de régions"
    )
    # La couverture reste celle du document réel, mention comprise — une page
    # avec mention porte du TEXTE, donc elle ne se cache pas en `pages_vides`.
    assert "thot:couverture chaine=ocr pages_attendues=2 pages_traitees=2" in contenu

    with _session(monde) as db:
        etat_lu = etat.lire(db.query(Artifact).filter(Artifact.id == artefact).one())
    # Le partiel vit dans l'ÉTAT prescrit (vocabulaire état.py), nommé ici :
    assert etat_lu.a_texte, (
        f"état {etat_lu.etat!r} : un partiel avec texte reste lisible"
    )
    assert "traitement partiel" in etat_lu.raison, etat_lu.raison
    assert "page 1 non reconnue" in etat_lu.raison, etat_lu.raison


def test_aucune_page_en_panne_ne_pretend_le_partiel(monde, tmp_path, monkeypatch):
    """Symétrique : sans panne, l'état ne s'affuble pas d'un « traitement partiel ».

    Le motif partiel ne vient que des mentions rendues (relites au vocabulaire
    de `markdown_pdf`) — jamais d’un état de mieux, jamais d'un décompte
    deviné. Une ingestion complète doit rester une ingestion complète.
    """

    def _detect(png: bytes, **k):
        return [{"label": "text", "score": 0.9, "box": [0, 0, 100, 40]}]

    def _parse(png: bytes, *, task="parse", **k):
        return "RAPPORT COMPLET les deux pages sont reconnues intégralement."

    monkeypatch.setattr("backend.ingestion.paddle_client.detect_regions", _detect)
    monkeypatch.setattr("backend.ingestion.paddle_client.parse_png", _parse)

    artefact = _deposer(monde, pdf_scanne_deux_pages(tmp_path), "scan.pdf")
    attachment_ingest._ingérer_un(artefact)

    observation = _observation(monde, artefact)
    contenu = str(observation.payload_ref or "")
    assert "non reconnue" not in contenu
    assert contenu.count("## Page") == 2
    with _session(monde) as db:
        etat_lu = etat.lire(db.query(Artifact).filter(Artifact.id == artefact).one())
    assert etat_lu.a_texte
    assert etat_lu.etat == etat.ETAT_EXTRAIT, (
        f"un succès complet doit écrire `extrait`, pas {etat_lu.etat!r}"
    )
    assert "traitement partiel" not in etat_lu.raison, etat_lu.raison


def test_le_plafond_de_pages_ecrit_tronque_et_pages_restantes(monde):
    """68 pages natives : 20 traitées, état `tronque`, `pages_restantes=48`.

    La borne de pages est une limite de conception, pas une panne : le texte
    des 20 pages reste servi, mais l'état doit dire `tronque` — jamais
    `extrait` — et la couverture doit exposer les 48 pages non traitées.
    """
    import pymupdf

    document = pymupdf.open()
    for index in range(68):
        page = document.new_page()
        page.insert_text(
            (72, 110),
            f"PAGE-{index + 1:02d} contenu suffisamment long pour rester natif.",
            fontsize=14,
        )
    octets = document.tobytes()
    document.close()

    artefact = _deposer(monde, octets, "long.pdf")
    attachment_ingest._ingérer_un(artefact)

    observation = _observation(monde, artefact)
    contenu = str(observation.payload_ref or "")
    assert "pages_restantes=48" in contenu, contenu[:400]
    assert "## Page 20" in contenu and "## Page 21" not in contenu

    with _session(monde) as db:
        etat_lu = etat.lire(db.query(Artifact).filter(Artifact.id == artefact).one())
    assert etat_lu.etat == etat.ETAT_TRONQUE, (
        f"un document coupé à 20 pages doit être `tronque`, pas {etat_lu.etat!r}"
    )
    assert etat_lu.a_texte, "le texte des 20 pages reste servi"
    assert "48 page" in etat_lu.raison, etat_lu.raison
    assert "tronqué" in etat_lu.raison, etat_lu.raison
