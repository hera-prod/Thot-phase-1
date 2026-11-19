"""Phase 1 du durcissement : intégrité d'ingestion et sécurité du staging.

Ces tests verrouillent les règles qui ne doivent plus dépendre de la chance :

  - une extraction est une TENTATIVE append-only : `ok=False` n'écrit qu'un audit ;
  - seul `ok=True` alimente fiche, chunks, vecteurs et FTS ;
  - le dernier `completed` survit à une série de refus ;
  - `binaire_refuse`, `obsolète` et `vide` cessent d'être servis par tous les lecteurs ;
  - `echec` legacy ne masque pas un texte déjà extrait ;
  - une observation citée par `memory_entries.evidence_ids` n'est jamais purgée ;
  - un drop refuse les chemins arbitraires, les liens sortants et les fichiers géants
    AVANT lecture ;
  - le staging est purgé même quand une validation précoce échoue.

Montage : base SQLite éphémère, racines de données jetables, dépôt par le vrai
chemin (`stage_picker_bytes` puis `attachments_prepare`). Aucun modèle, aucun GPU,
aucun réseau.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text as sql_text
from sqlalchemy.orm import sessionmaker

from backend.db import database as database_module
from backend.db.models import (
    Artifact,
    ArtifactCard,
    ArtifactChunk,
    Base,
    Blob,
    Conversation,
    MemoryEntry,
    Observation,
)
from backend.inference import loop as L
from backend.ingestion import etat as ETAT
from backend.ipc import attachments as A
from backend.search import chunks as CH
from backend.search import fts as FTS
from backend.search import reader
from backend.search import observations as OBS
from backend.search.observations import (
    record_extraction_observation,
    record_tool_observation,
)
from backend.services import espaces


# ---------------------------------------------------------------------------
# Montage
# ---------------------------------------------------------------------------


@pytest.fixture()
def monde(tmp_path, monkeypatch):
    moteur = create_engine(f"sqlite:///{tmp_path / 'durcissement.db'}")
    Base.metadata.create_all(moteur)
    from backend.db.database import _ensure_fts

    _ensure_fts(moteur)
    sessions = sessionmaker(bind=moteur, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(database_module, "SessionLocal", sessions)
    racine = tmp_path / "data"
    (racine / "blobs").mkdir(parents=True)
    (racine / "temp").mkdir(parents=True)
    monkeypatch.setenv("THOT_PORTABLE", "1")
    monkeypatch.setenv("THOT_APP_DATA_DIR", str(racine))
    monkeypatch.setattr(A, "_declencher_ingestion", lambda ids: None)
    with sessions() as db:
        CH.ensure_chunk_tables(db)
        db.commit()
    return sessions


def _discussion(sessions) -> str:
    conversation = f"conv_{uuid.uuid4().hex[:12]}"
    with sessions() as db:
        db.add(Conversation(id=conversation, title="Phase 1", mode="chat"))
        db.commit()
    return conversation


def _deposer(sessions, nom: str, octets: bytes) -> tuple[str, str]:
    conversation = _discussion(sessions)
    with sessions() as db:
        item = A.stage_picker_bytes(
            db, {"requestId": "phase1", "filename": nom, "bytes": octets}
        )["items"][0]
        artefact = A.attachments_prepare(
            db,
            {"conversationId": conversation, "stageItemIds": [item["stage_item_id"]]},
        )["artifact_ids"][0]
    return conversation, artefact


def _deposer_plusieurs(sessions, fichiers: list[tuple[str, bytes]]) -> list[str]:
    conversation = _discussion(sessions)
    with sessions() as db:
        items = [
            A.stage_picker_bytes(
                db, {"requestId": f"phase1-{i}", "filename": nom, "bytes": octets}
            )["items"][0]
            for i, (nom, octets) in enumerate(fichiers)
        ]
        return A.attachments_prepare(
            db,
            {
                "conversationId": conversation,
                "stageItemIds": [item["stage_item_id"] for item in items],
            },
        )["artifact_ids"]


TEXTE_UTILE = (
    "Contrat de prestation. Montant total : 1 200,00 euros. "
    "Signataire : Atelier Beziers. La prestation commence le 12 mars 2026. "
) * 8


def _observation(sessions, artifact_id: str, texte: str, *, ok: bool):
    return record_extraction_observation(
        artifact_id,
        "attachment_ingest",
        None,
        texte,
        ok=ok,
        tool_version="test/phase1",
    )


def _compter(sessions, modele, *criteres) -> int:
    with sessions() as db:
        requete = db.query(modele)
        for critere in criteres:
            requete = requete.filter(critere)
        return requete.count()


# ---------------------------------------------------------------------------
# 1. Append-only et indexation conditionnée à ok=True
# ---------------------------------------------------------------------------


def test_un_refus_n_ecrit_aucune_fiche_ni_index(monde):
    with monde() as db:
        conversation = _discussion(monde)
    _, artefact = _deposer(monde, "note.txt", TEXTE_UTILE.encode("utf-8"))

    observation = _observation(monde, artefact, "contenu refusé", ok=False)

    assert observation is not None and observation.status == "failed"
    assert _compter(monde, ArtifactCard, ArtifactCard.artifact_id == artefact) == 0
    assert _compter(monde, ArtifactChunk, ArtifactChunk.artifact_id == artefact) == 0
    with monde() as db:
        assert (
            db.execute(
                sql_text("SELECT COUNT(*) FROM artifact_search WHERE artifact_id=:a"),
                {"a": artefact},
            ).scalar()
            == 0
        )
        assert (
            db.execute(
                sql_text("SELECT COUNT(*) FROM artifact_chunk_vectors"), {}
            ).scalar()
            == 0
        )


def test_deux_tentatives_identiques_creent_deux_lignes(monde):
    _, artefact = _deposer(monde, "note.txt", TEXTE_UTILE.encode("utf-8"))
    premier = _observation(monde, artefact, TEXTE_UTILE, ok=True)
    second = _observation(monde, artefact, TEXTE_UTILE, ok=True)

    assert premier is not None and second is not None
    assert premier.id != second.id
    assert _compter(monde, Observation, Observation.artifact_id == artefact) == 2


def test_le_depot_indexe_une_fiche_et_des_fragments_si_ok(monde):
    _, artefact = _deposer(monde, "contrat.txt", TEXTE_UTILE.encode("utf-8"))
    _observation(monde, artefact, TEXTE_UTILE, ok=True)

    assert _compter(monde, ArtifactCard, ArtifactCard.artifact_id == artefact) >= 1
    with monde() as db:
        actives = (
            db.query(ArtifactCard)
            .filter(
                ArtifactCard.artifact_id == artefact,
                ArtifactCard.is_current == 1,
            )
            .count()
        )
    assert actives == 1
    assert _compter(monde, ArtifactChunk, ArtifactChunk.artifact_id == artefact) > 0
    with monde() as db:
        assert (
            db.execute(
                sql_text("SELECT COUNT(*) FROM artifact_search WHERE artifact_id=:a"),
                {"a": artefact},
            ).scalar()
            > 0
        )


def test_record_tool_observation_extraction_en_echec_n_indexe_rien(monde):
    """L'ancien chemin d'extraction doit suivre la même doctrine que le nouveau."""
    _, artefact = _deposer(monde, "note.txt", TEXTE_UTILE.encode("utf-8"))

    observation = record_tool_observation(
        artefact, "doc_ocr", "lire", "contenu refusé", False
    )

    assert observation is not None and observation.status == "failed"
    assert _compter(monde, ArtifactCard, ArtifactCard.artifact_id == artefact) == 0
    assert _compter(monde, ArtifactChunk, ArtifactChunk.artifact_id == artefact) == 0
    with monde() as db:
        assert (
            db.execute(
                sql_text("SELECT COUNT(*) FROM artifact_search WHERE artifact_id=:a"),
                {"a": artefact},
            ).scalar()
            == 0
        )
        assert (
            db.execute(
                sql_text("SELECT COUNT(*) FROM artifact_chunk_vectors"), {}
            ).scalar()
            == 0
        )


def test_collision_de_blob_deux_observations_distinctes(monde):
    """Deux artefacts partageant un blob ne partagent pas leur observation."""
    premier, second = _deposer_plusieurs(
        monde,
        [
            ("partage-a.txt", TEXTE_UTILE.encode("utf-8")),
            ("partage-b.txt", TEXTE_UTILE.encode("utf-8")),
        ],
    )
    with monde() as db:
        assert (
            db.query(Blob)
            .filter(Blob.id == db.get(Artifact, premier).blob_id)
            .one()
            .sha256
            == db.query(Blob)
            .filter(Blob.id == db.get(Artifact, second).blob_id)
            .one()
            .sha256
        )

    observation_premier = record_tool_observation(
        premier, "doc_ocr", "lire", TEXTE_UTILE, True
    )
    observation_second = record_tool_observation(
        second, "doc_ocr", "lire", TEXTE_UTILE, True
    )

    assert observation_premier is not None and observation_second is not None
    assert observation_premier.id != observation_second.id
    assert observation_premier.artifact_id == premier
    assert observation_second.artifact_id == second
    assert _compter(monde, Observation, Observation.artifact_id == premier) == 1
    assert _compter(monde, Observation, Observation.artifact_id == second) == 1


def test_reingestion_reussie_apres_un_echec_de_meme_empreinte(monde):
    """Un `failed` ne doit pas masquer le succès ultérieur de même empreinte."""
    _, artefact = _deposer(monde, "contrat.txt", TEXTE_UTILE.encode("utf-8"))

    echec = record_tool_observation(
        artefact, "attachment_ingest", None, "extraction impossible", False
    )
    succes = record_tool_observation(
        artefact, "attachment_ingest", None, TEXTE_UTILE, True
    )

    assert echec is not None and echec.status == "failed"
    assert succes is not None and succes.status == "completed"
    assert succes.id != echec.id
    assert _compter(monde, Observation, Observation.artifact_id == artefact) == 2
    assert _compter(monde, ArtifactCard, ArtifactCard.artifact_id == artefact) >= 1
    assert _compter(monde, ArtifactChunk, ArtifactChunk.artifact_id == artefact) > 0
    with monde() as db:
        assert (
            db.execute(
                sql_text("SELECT COUNT(*) FROM artifact_search WHERE artifact_id=:a"),
                {"a": artefact},
            ).scalar()
            > 0
        )


# ---------------------------------------------------------------------------
# 2. Rétention : dernier completed et observations citées
# ---------------------------------------------------------------------------


def test_un_succes_entoure_de_six_refus_reste_servi(monde):
    _, artefact = _deposer(monde, "contrat.txt", TEXTE_UTILE.encode("utf-8"))
    _observation(monde, artefact, TEXTE_UTILE, ok=True)
    for _ in range(6):
        _observation(monde, artefact, "refus binaire", ok=False)

    with monde() as db:
        correspondances = (
            db.query(Observation)
            .filter(Observation.artifact_id == artefact)
            .order_by(Observation.created_at.asc())
            .all()
        )
        complets = [o for o in correspondances if o.status == "completed"]
        assert len(complets) == 1, "le dernier completed doit survivre à la rétention"
        texte, observation_id = ETAT.texte_ingeste(db, db.get(Artifact, artefact))
    assert texte and texte.startswith("Contrat de prestation")
    assert observation_id == complets[0].id
    # 5 non citées récentes ∪ le completed : la plus ancienne des refus part.
    assert len(correspondances) == 6


def test_une_observation_citee_n_est_jamais_purgee(monde):
    _, artefact = _deposer(monde, "note.txt", TEXTE_UTILE.encode("utf-8"))
    ancienne = _observation(monde, artefact, "première tentative", ok=False)
    assert ancienne is not None

    with monde() as db:
        espace = espaces.create_workspace(db, "Preuves")
        db.add(
            MemoryEntry(
                id=f"mem_{uuid.uuid4().hex[:12]}",
                entry_key=f"cle_{uuid.uuid4().hex[:8]}",
                workspace_id=str(espace.id),
                content="Une affirmation appuyée sur la première tentative.",
                evidence_ids=[ancienne.id],
            )
        )
        db.commit()

    for _ in range(8):
        _observation(monde, artefact, "refus suivant", ok=False)

    with monde() as db:
        assert db.get(Observation, ancienne.id) is not None, (
            "une preuve citée ne doit jamais être purgée par la rétention"
        )


def test_citations_illisibles_annulent_la_purge(monde, monkeypatch):
    """Fail-closed : sans liste des citations, on ne supprime rien."""
    _, artefact = _deposer(monde, "note.txt", TEXTE_UTILE.encode("utf-8"))
    monkeypatch.setattr(OBS, "_observations_citees", lambda db: None)
    for _ in range(8):
        _observation(monde, artefact, "refus suivant", ok=False)

    assert _compter(monde, Observation, Observation.artifact_id == artefact) == 8, (
        "une lecture des citations en échec doit annuler toute purge"
    )


# ---------------------------------------------------------------------------
# 3. Contrats d'état
# ---------------------------------------------------------------------------


def test_verdict_inconnu_leve(monde):
    with pytest.raises(ETAT.VerdictInconnu, match="inconnu"):
        ETAT.verdict_vers_etat({"extract": "peut-etre"})
    with pytest.raises(ETAT.VerdictInconnu):
        ETAT.verdict_vers_etat({})


def test_marquer_un_artefact_absent_leve(monde):
    with monde() as db:
        with pytest.raises(ETAT.ArtefactAbsent, match="artefact absent"):
            ETAT.marquer(db, "art_inexistant", ETAT.ETAT_EXTRAIT, "jamais écrit")


# ---------------------------------------------------------------------------
# 4. Lecture : états sans texte et echec legacy
# ---------------------------------------------------------------------------


def _assert_non_servi(sessions, artefact: str):
    with sessions() as db:
        art = db.get(Artifact, artefact)
        texte, preuve = ETAT.texte_ingeste(db, art)
        assert texte is None and preuve is None
        assert CH.best_source_text(db, art) == ""
        refus = reader.read_payload(db, artefact, {artefact})
        assert refus["ok"] is False


@pytest.mark.parametrize("valeur", ["binaire_refuse", "obsolète", "vide"])
def test_les_etats_sans_texte_ne_sont_servis_par_aucun_lecteur(monde, valeur):
    conversation, artefact = _deposer(monde, "contrat.txt", TEXTE_UTILE.encode("utf-8"))
    _observation(monde, artefact, TEXTE_UTILE, ok=True)

    with monde() as db:
        ETAT.marquer(db, artefact, valeur, "état de test")

    _assert_non_servi(monde, artefact)

    resultat = L._execute_ask_files(
        {"args": {"questions": ["Quel est le montant ?"], "resource_ids": ["doc_1"]}},
        None,
        conv_id=conversation,
        request_id="phase1",
    )
    assert resultat["ok"] is True
    assert "1 200,00" not in resultat["result"]
    assert ETAT.PHRASES[valeur] in resultat["result"]


def test_un_echec_legacy_ne_masque_pas_un_texte_complet(monde):
    conversation, artefact = _deposer(monde, "contrat.txt", TEXTE_UTILE.encode("utf-8"))
    _observation(monde, artefact, TEXTE_UTILE, ok=True)

    with monde() as db:
        db.query(Artifact).filter(Artifact.id == artefact).update(
            {"ingest_state": ETAT.ETAT_ECHEC, "ingest_reason": "échec historique"},
            synchronize_session=False,
        )
        db.commit()

    with monde() as db:
        art = db.get(Artifact, artefact)
        texte, _ = ETAT.texte_ingeste(db, art)
        assert texte and "1 200,00" in texte
        assert CH.best_source_text(db, art).startswith("Contrat de prestation")
        lecture = reader.read_payload(db, artefact, {artefact})
        assert lecture["ok"] and "1 200,00" in lecture["excerpt"]

    resultat = L._execute_ask_files(
        {"args": {"questions": ["Quel est le montant ?"], "resource_ids": ["doc_1"]}},
        None,
        conv_id=conversation,
        request_id="phase1",
    )
    assert "1 200,00" in resultat["result"], (
        "un echec postérieur à un succès ne doit pas effacer le texte servi"
    )


# ---------------------------------------------------------------------------
# 4. Binaire refusé : purge et backfill
# ---------------------------------------------------------------------------


def test_binaire_refuse_purge_l_index_et_le_backfill_compte(monde):
    _, artefact = _deposer(monde, "contrat.txt", TEXTE_UTILE.encode("utf-8"))
    _observation(monde, artefact, TEXTE_UTILE, ok=True)
    with monde() as db:
        chunk_id = (
            db.query(ArtifactChunk.id)
            .filter(ArtifactChunk.artifact_id == artefact)
            .scalar()
        )
        db.execute(
            sql_text(
                "INSERT OR REPLACE INTO artifact_chunk_vectors(chunk_id, dim, vector)"
                " VALUES (:c, 4, :v)"
            ),
            {"c": chunk_id, "v": b"test"},
        )
        db.commit()
        ETAT.marquer(db, artefact, ETAT.ETAT_BINAIRE, "contenu non textuel")

    with monde() as db:
        bilan = FTS.backfill_binaires_refuses(db)
    assert bilan["traites"] == 1
    assert _compter(monde, ArtifactChunk, ArtifactChunk.artifact_id == artefact) == 0
    with monde() as db:
        assert (
            db.execute(
                sql_text("SELECT COUNT(*) FROM artifact_search WHERE artifact_id=:a"),
                {"a": artefact},
            ).scalar()
            == 0
        )
        assert (
            db.execute(
                sql_text("SELECT COUNT(*) FROM artifact_chunk_vectors"), {}
            ).scalar()
            == 0
        )
    # Idempotent : une seconde passe traite encore la même ligne, sans erreur.
    with monde() as db:
        assert FTS.backfill_binaires_refuses(db)["traites"] == 1


# ---------------------------------------------------------------------------
# 5. Blob partagé : l'enrichissement suit l'artefact
# ---------------------------------------------------------------------------


def test_un_blob_partage_entre_deux_artefacts_n_enrichit_que_le_bon(monde):
    conversation = _discussion(monde)
    with monde() as db:
        items = [
            A.stage_picker_bytes(
                db,
                {
                    "requestId": f"partage-{i}",
                    "filename": "partage.txt",
                    "bytes": TEXTE_UTILE.encode("utf-8"),
                },
            )["items"][0]
            for i in range(2)
        ]
        artefacts = A.attachments_prepare(
            db,
            {
                "conversationId": conversation,
                "stageItemIds": [item["stage_item_id"] for item in items],
            },
        )["artifact_ids"]
    premier, second = artefacts
    with monde() as db:
        assert (
            db.query(Blob)
            .filter(Blob.id == db.get(Artifact, premier).blob_id)
            .one()
            .sha256
            == db.query(Blob)
            .filter(Blob.id == db.get(Artifact, second).blob_id)
            .one()
            .sha256
        )

    _observation(monde, premier, TEXTE_UTILE, ok=True)

    assert _compter(monde, ArtifactCard, ArtifactCard.artifact_id == premier) >= 1
    assert _compter(monde, ArtifactCard, ArtifactCard.artifact_id == second) == 0
    assert _compter(monde, ArtifactChunk, ArtifactChunk.artifact_id == premier) > 0
    assert _compter(monde, ArtifactChunk, ArtifactChunk.artifact_id == second) == 0


# ---------------------------------------------------------------------------
# 6. Chemins du drop
# ---------------------------------------------------------------------------


def test_chemin_arbitraire_refuse(monde):
    with monde() as db:
        resultat = A.stage_dropped_paths(
            db, {"requestId": "phase1", "paths": ["/etc/hostname"]}
        )
    assert resultat["items"] == [] and len(resultat["rejected"]) == 1
    assert "racines autorisées" in resultat["rejected"][0]["message"]


def test_symlink_sortant_refuse(monde, tmp_path):
    cible = tmp_path / "lien.txt"
    cible.symlink_to("/etc/hostname")
    with monde() as db:
        resultat = A.stage_dropped_paths(
            db, {"requestId": "phase1", "paths": [str(cible)]}
        )
    assert resultat["items"] == [] and len(resultat["rejected"]) == 1


def test_fichier_geant_refuse_avant_lecture(monde, tmp_path, monkeypatch):
    geant = tmp_path / "geant.txt"
    with open(geant, "wb") as fichier:
        fichier.seek(A.MAX_UPLOAD_BYTES)
        fichier.write(b"x")

    appels: list[str] = []

    def _read_bytes(chemin):
        appels.append(str(chemin))
        raise AssertionError(
            "read_bytes ne doit pas être appelé avant le contrôle de taille"
        )

    monkeypatch.setattr(Path, "read_bytes", _read_bytes)
    with monde() as db:
        resultat = A.stage_dropped_paths(
            db, {"requestId": "phase1", "paths": [str(geant)]}
        )
    assert resultat["items"] == [] and len(resultat["rejected"]) == 1
    assert "trop volumineux" in resultat["rejected"][0]["message"]
    assert appels == []


# ---------------------------------------------------------------------------
# 7. Staging : purge même sur validation précoce
# ---------------------------------------------------------------------------


def test_staging_purge_sur_validation_precoce(monde):
    with monde() as db:
        item = A.stage_picker_bytes(
            db,
            {
                "requestId": "phase1",
                "filename": "note.txt",
                "bytes": TEXTE_UTILE.encode("utf-8"),
            },
        )["items"][0]
        conversation = _discussion(monde)
        with pytest.raises(ValueError, match="`names` compte"):
            A.attachments_prepare(
                db,
                {
                    "conversationId": conversation,
                    "stageItemIds": [item["stage_item_id"]],
                    "names": ["un", "deux"],
                },
            )

    dossier = A._staged_dir()
    restants = list(dossier.glob(f"{item['sha256']}*"))
    assert restants == [], "un refus précoce ne doit pas laisser le fichier stagé"


def test_purge_ttl_du_staging(monde):
    with monde() as db:
        item = A.stage_picker_bytes(
            db,
            {
                "requestId": "phase1",
                "filename": "vieux.txt",
                "bytes": TEXTE_UTILE.encode("utf-8"),
            },
        )["items"][0]
    dossier = A._staged_dir()
    fichier = next(dossier.glob(f"{item['sha256']}*"))
    ancien = fichier.stat().st_mtime - A.STAGING_TTL_S - 60
    import os

    os.utime(fichier, (ancien, ancien))
    assert A.purger_staging_expires() == 1
    assert not fichier.exists()
