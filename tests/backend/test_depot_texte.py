"""Le dépôt accepte `.txt` et `.md` — et un fichier texte dépisté devient interrogeable.

LE DÉFAUT VERROUILLÉ ICI : la branche `kind == "text"` de
`backend/ingestion/attachment_ingest.extraire_texte` était inatteignable.
`ALLOWED_SUFFIXES` et `sniff_mime` ne produisaient que `image`, `pdf`, `sheet`
— un `.txt` déposé se faisait refuser « Extension non autorisée », et la
moitié du contrat texte (fiche `docs/intentions/mecanismes/ingestion/texte.md`
§1.2 : « Les extensions `.txt`, `.md` sont des exemples mentionnés ») restait
du code mort.

Ce que chaque test atteste :

  - un `.md` et un `.txt` déposés par le VRAI chemin de dépôt
    (`stage_picker_bytes` → `attachments_prepare`) produisent `kind="text"`,
    honnêtement — pas un tableur maquillé, pas un `octet-stream` ;
  - l'ingestion passe par le chemin commun : observation, fiche, fragments,
    ligne d'index — et la recherche retrouve le contenu par ses mots ;
  - la garde EXISTANTE est conservée : une extension hors liste est rejetée
    EN NOMMANT l'extension, et un binaire nommé `.txt` est rejeté au contenu ;
  - la signature prime toujours : un PDF nommé `.txt` est refusé.

Base neuve et jetable par test, ingestion pilotée à la main, aucun réseau :
même outillage que `test_ingest_images.py`, dont ce fichier est le prolongement
au texte.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, text as sql_text
from sqlalchemy.orm import sessionmaker

from backend.db import database as database_module
from backend.db.models import (
    Artifact,
    ArtifactChunk,
    Base,
    Observation,
)
from backend.ingestion import attachment_ingest
from backend.ipc import attachments as A
from backend.ipc import paths_bridge as paths_bridge_module

# Un contenu texte ASSEZ LONG pour franchir les seuils d'ingestion (200
# caractères pour produire des fragments), et porteur d'un mot distinctif
# qu'aucun autre fichier du test ne contient : la recherche doit le trouver
# dans CE fichier, pas dans un voisin.
CONTENU_MD = (
    "# Rapport d'achalandage\n\n## Conclusion\n\nLe rayon pythagorique du dépôt de "
    "Béziers est complet : palettes comptées, casse inférieure à un pour cent, "
    "rotation assurée chaque matin avant sept heures, réassort commandé la veille "
    "au soir et vérifié à la réception par le chef de quai en personne.\n"
)
CONTENU_TXT = (
    "COMPTE RENDU\nLe rayon pythagorique du dépôt de Béziers est complet : palettes "
    "comptées, casse inférieure à un pour cent, rotation assurée chaque matin, "
    "réassort commandé la veille et vérifié à la réception par le chef de quai.\n"
)


@contextmanager
def _session(sessions):
    db = sessions()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture()
def monde(tmp_path, monkeypatch):
    """Base neuve, index FTS et tables de fragments, racines de données jetables.

    Le déclencheur d'ingestion est neutralisé : le dépôt lance un fil, et un fil
    survivant au test écrirait dans la base du suivant. `_ingérer_un` est
    piloté à la main, au moment choisi.
    """
    moteur = create_engine(f"sqlite:///{tmp_path / 'depot-texte.db'}")
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
    monkeypatch.setattr(paths_bridge_module, "blobs_root", lambda: racine / "blobs", raising=False)
    monkeypatch.setattr(paths_bridge_module, "temp_root", lambda: racine / "temp", raising=False)
    monkeypatch.setattr(
        paths_bridge_module, "data_dirs", lambda: (racine, racine / "temp"), raising=False
    )
    monkeypatch.setattr(A, "_declencher_ingestion", lambda ids: None, raising=False)
    with _session(sessions) as db:
        ensure_chunk_tables(db)
        db.commit()
    return sessions


def _deposer(sessions, octets: bytes, nom: str) -> tuple[str, str]:
    """Dépose par le VRAI chemin de dépôt. Renvoie (conversation, artefact)."""
    conversation = f"conv_{uuid.uuid4().hex[:12]}"
    with _session(sessions) as db:
        db.execute(
            sql_text("INSERT INTO conversations (id,title,mode,created_at,updated_at)"
                     " VALUES (:c,'depot-texte','chat',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"),
            {"c": conversation},
        )
        db.commit()
        item = A.stage_picker_bytes(
            db, {"requestId": "depot-texte", "filename": nom, "bytes": octets}
        )["items"][0]
        (artefact,) = A.attachments_prepare(
            db, {"conversationId": conversation, "stageItemIds": [item["stage_item_id"]],
                 "names": [nom]}
        )["artifact_ids"]
    return conversation, artefact


def _kind(sessions, artefact: str) -> str:
    with _session(sessions) as db:
        return str(db.get(Artifact, artefact).kind)


def _observations(sessions, artefact: str) -> list[Observation]:
    with _session(sessions) as db:
        return db.query(Observation).filter(
            Observation.artifact_id == artefact,
            Observation.tool_name == "attachment_ingest",
        ).all()


@pytest.mark.parametrize("nom,contenu", [
    ("notes.md", CONTENU_MD),
    ("notes.txt", CONTENU_TXT),
], ids=["markdown", "texte-brut"])
def test_un_fichier_texte_depose_est_ingere_et_retrouvable(monde, nom, contenu):
    """Un `.md`/`.txt` déposé produit kind `text` ET une ingestion complète.

    La chaîne est celle d'une pièce jointe ordinaire — staging, promotion,
    observation, fiche, fragments, index. Un fichier qui se déposerait sans
    devenir interrogeable serait exactement le dépôt « réussi mais muet » que
    le reste de la suite interdit.
    """
    conversation, artefact = _deposer(monde, contenu.encode("utf-8"), nom)
    assert _kind(monde, artefact) == "text"

    attachment_ingest._ingérer_un(artefact)

    lignes = _observations(monde, artefact)
    assert len(lignes) == 1 and lignes[0].status == "completed", (
        "un fichier texte doit s'ingérer sans modèle, donc intégralement"
    )
    with _session(monde) as db:
        from backend.search import scope as _scope

        fragments = db.query(ArtifactChunk).filter(
            ArtifactChunk.artifact_id == artefact).all()
        assert fragments, "un texte ingéré doit produire des fragments"
        assert artefact in _scope.resolve_allowed_artifacts(conversation), (
            "la ressource doit être dans le périmètre de SA discussion"
        )
        # La recherche retrouve le contenu par ses mots — le but de l'ingestion.
        from backend.search import fts as _fts

        page = _fts.search_scopes(
            db, query="rayon pythagorique", conversation_id=conversation,
            allowed_ids={artefact})
        trouves = [r.get("artifact_id") for r in (page.get("results") or [])]
        assert artefact in trouves, (
            f"le contenu du fichier n'est pas retrouvable : {page.get('results')}"
        )


@pytest.mark.parametrize("nom,octets", [
    ("archive.zip", b"PK\x03\x04" + b"\x00" * 8),
    ("source.rst", b"Titre du document\n==========\n"),
    ("donnees.json", b'{"cle": "valeur"}\n'),
], ids=["zip", "rst", "json"])
def test_une_extension_hors_liste_est_rejetee_en_nommant(monde, nom, octets):
    """La garde d'entrée existe toujours : hors liste, le refus NOMME l'extension.

    Ouvrir `.txt`/`.md` ne doit rien ouvrir d'autre : une liste qui s'élargit
    en silence serait une entrée sans garde. Le refus passe par le VRAI chemin
    de staging, pas par `sniff_mime` seul — c'est le chemin réel qui décide.
    """
    with _session(monde) as db:
        with pytest.raises(ValueError) as attrape:
            A.stage_picker_bytes(
                db, {"requestId": "depot-texte", "filename": nom, "bytes": octets}
            )
    message = str(attrape.value)
    assert "Extension non autorisée" in message, message
    assert "Attendu" in message, message


def test_un_binaire_nomme_txt_est_refuse_au_contenu():
    """`.txt` n'a pas de signature : le contenu seul décide, comme pour le `.csv`.

    Un ratio de bytes de contrôle élevé dit « ce fichier n'est pas du texte » ;
    le refuser ici vaut mieux qu'un `kind="text"` mensonger sur un binaire.
    """
    with pytest.raises(ValueError, match="binaire"):
        A.sniff_mime(bytes(range(32)) + b"\x00\x01\x02", "notes.txt")


def test_la_signature_prime_toujours_sur_lextension():
    """Un PDF nommé `.txt` est refusé : l'extension n'est jamais une preuve."""
    import pymupdf

    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 100), "CONTRAT", fontsize=14)
    with pytest.raises(ValueError, match="ne correspond pas"):
        A.sniff_mime(document.tobytes()[:12], "rapport.txt")
