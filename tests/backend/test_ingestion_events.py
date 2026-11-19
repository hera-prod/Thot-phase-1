"""Phase 6 — observabilité de l'ingestion et reprise utilisateur.

Ce fichier verrouille trois contrats qui se répondent :

  · un événement de démarrage suit l'écriture de `en_cours`, jamais l'enfilage ;
  · une exécution produit UN SEUL terminal (`finished` ou `failed`), et une perte
    de bail n'en produit aucun ;
  · la relance demandée par l'utilisateur vérifie l'appartenance au dépôt, l'état
    et le single-flight avant d'enfiler — un doublon ne se raconte pas « relancé ».

Aucun OCR, aucun GPU, aucun réseau : l'extraction est simulée et les événements
sont capturés par un faux `emit`.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from test_depot_integrite import base_depot  # noqa: F401 — fixture partagée

from backend.db import database as database_module
from backend.db.models import (
    Artifact,
    ArtifactLink,
    Base,
    Blob,
    Conversation,
    Workspace,
)
from backend.ingestion import attachment_ingest
from backend.ingestion import etat as ETAT
from backend.ingestion.erreurs import BailPerdu
from backend.ingestion.paddle_client import ModeleIndisponible

# Capturée AVANT que la fixture `base` ne la remplace par une lambda : le test du
# doublon doit éprouver la VRAIE admission, pas son substitut.
_LANCER_INGESTION_ORIGINAL = attachment_ingest.lancer_ingestion


@pytest.fixture()
def base(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'evenements.db'}")
    Base.metadata.create_all(bind=engine)
    from backend.db.database import _ensure_fts

    _ensure_fts(engine)
    sessions = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    monkeypatch.setattr(database_module, "SessionLocal", sessions)
    racine = tmp_path / "data"
    (racine / "blobs").mkdir(parents=True)
    monkeypatch.setenv("THOT_PORTABLE", "1")
    monkeypatch.setenv("THOT_APP_DATA_DIR", str(racine))
    monkeypatch.setattr(
        attachment_ingest, "lancer_ingestion", lambda ids, **kw: list(ids)
    )
    return sessions, racine


@pytest.fixture()
def evenements(monkeypatch):
    from backend.inference import ipc

    vus: list[tuple[str, dict, dict]] = []

    def faux_emit(event, payload, **kwargs):
        vus.append((event, payload, kwargs))
        return True

    monkeypatch.setattr(ipc, "emit", faux_emit)
    return vus


def _espace(sessions) -> str:
    marque = uuid.uuid4().hex[:10]
    with sessions() as db:
        db.add(Workspace(id=f"ws_{marque}", name=f"Espace {marque}", state="active"))
        db.commit()
    return f"ws_{marque}"


def _creer(
    sessions,
    racine: Path,
    *,
    etat: str = ETAT.ETAT_DEPOSE,
    workspace: str | None = None,
    conversation: str | None = None,
    supprime: bool = False,
) -> str:
    """Un artefact avec un blob réel et UN lien actif (espace ou conversation)."""
    marque = uuid.uuid4().hex[:12]
    artifact_id = f"art_{marque}"
    blob_id = f"blob_{marque}"
    relatif = f"blobs/{blob_id}.bin"
    fichier = racine / relatif
    fichier.parent.mkdir(parents=True, exist_ok=True)
    fichier.write_bytes(b"contenu de test")
    with sessions() as db:
        db.add(
            Blob(
                id=blob_id,
                sha256=marque,
                relative_path=relatif,
                size_bytes=len(b"contenu de test"),
                mime_detected="text/plain",
                state="ready",
            )
        )
        db.add(
            Artifact(
                id=artifact_id,
                blob_id=blob_id,
                kind="text",
                original_filename="notes.txt",
                display_name="notes.txt",
                ingest_state=etat,
                ingest_reason="déposé, extraction en attente",
                deleted_at=ETAT.utc_naif() if supprime else None,
            )
        )
        if conversation is not None:
            db.add(
                Conversation(
                    id=conversation,
                    title="Discussion",
                    mode="chat",
                    workspace_id=workspace,
                )
            )
            scope_kind, scope_id = "conversation", conversation
        else:
            scope_kind, scope_id = "workspace", workspace or _espace(sessions)
        db.add(
            ArtifactLink(
                id=f"lnk_{marque}",
                artifact_id=artifact_id,
                scope_kind=scope_kind,
                scope_id=scope_id,
                role="shared" if scope_kind == "workspace" else "attachment",
            )
        )
        db.commit()
    return artifact_id


def _etats(sessions, artifact_id: str) -> tuple[Artifact, ETAT.Etat]:
    with sessions() as db:
        artefact = db.query(Artifact).filter(Artifact.id == artifact_id).one()
        return artefact, ETAT.lire(artefact)


# ---------------------------------------------------------------------------
# 6.1 — événements d'ingestion
# ---------------------------------------------------------------------------


def test_un_succes_emet_started_puis_un_seul_finished(base, evenements, monkeypatch):
    sessions, racine = base
    espace = _espace(sessions)
    artefact = _creer(sessions, racine, workspace=espace)
    texte = (
        "Compte rendu de la réunion, décisions prises et responsables nommés, "
        "avec un contenu suffisamment long pour être indexé. " * 4
    )
    monkeypatch.setattr(
        attachment_ingest, "extraire_texte", lambda *a, **k: (texte, "native")
    )

    attachment_ingest._ingérer_un(artefact, modeles_autorises=True)

    noms = [nom for nom, _, _ in evenements]
    assert noms == ["artifact_ingestion_started", "artifact_ingestion_finished"]
    depart = evenements[0][1]
    arrivee = evenements[1][1]
    assert depart["artifact_id"] == artefact
    assert depart["workspace_id"] == espace
    assert depart["state"] == ETAT.ETAT_EN_COURS
    assert arrivee["state"] == ETAT.ETAT_EXTRAIT
    assert arrivee["workspace_id"] == espace
    assert arrivee["retryable"] is False
    # Un seul terminal : le doublon `finished`/`failed` est le défaut que ce test tue.
    terminaux = [nom for nom in noms if nom != "artifact_ingestion_started"]
    assert terminaux == ["artifact_ingestion_finished"]
    # Les événements d'ingestion sont hors cycle de run.
    for _, _, kwargs in evenements:
        assert kwargs["run_id"] is None
        assert kwargs["conversation_id"] is None


def test_une_panne_emet_started_puis_failed(base, evenements, monkeypatch):
    sessions, racine = base
    espace = _espace(sessions)
    artefact = _creer(sessions, racine, workspace=espace)

    def panne(*args, **kwargs):
        raise RuntimeError("lecteur cassé")

    monkeypatch.setattr(attachment_ingest, "extraire_texte", panne)

    attachment_ingest._ingérer_un(artefact, modeles_autorises=True)

    assert [nom for nom, _, _ in evenements] == [
        "artifact_ingestion_started",
        "artifact_ingestion_failed",
    ]
    _, _, _ = evenements[1]
    payload = evenements[1][1]
    assert payload["state"] == ETAT.ETAT_ECHEC
    assert "lecteur cassé" in payload["reason"]
    assert payload["retryable"] is True


def test_un_modele_indisponible_emet_failed_sur_depose(base, evenements, monkeypatch):
    sessions, racine = base
    espace = _espace(sessions)
    artefact = _creer(sessions, racine, workspace=espace)

    def indisponible(*args, **kwargs):
        raise ModeleIndisponible("worker OCR : timeout de chargement")

    monkeypatch.setattr(attachment_ingest, "extraire_texte", indisponible)

    attachment_ingest._ingérer_un(artefact, modeles_autorises=True)

    assert [nom for nom, _, _ in evenements] == [
        "artifact_ingestion_started",
        "artifact_ingestion_failed",
    ]
    payload = evenements[1][1]
    assert payload["state"] == ETAT.ETAT_DEPOSE
    assert "indisponible" in payload["reason"]
    assert payload["retryable"] is True


def test_un_document_vide_emet_finished(base, evenements, monkeypatch):
    sessions, racine = base
    espace = _espace(sessions)
    artefact = _creer(sessions, racine, workspace=espace)
    monkeypatch.setattr(
        attachment_ingest, "extraire_texte", lambda *a, **k: ("   ", "native")
    )

    attachment_ingest._ingérer_un(artefact, modeles_autorises=True)

    assert [nom for nom, _, _ in evenements] == [
        "artifact_ingestion_started",
        "artifact_ingestion_finished",
    ]
    assert evenements[1][1]["state"] == ETAT.ETAT_VIDE


def test_l_espace_est_deduit_de_la_conversation(base, evenements, monkeypatch):
    """Un fichier déposé en discussion n'a pas de lien workspace : l'événement
    remonte à l'espace de la discussion, jamais à un identifiant deviné."""
    sessions, racine = base
    espace = _espace(sessions)
    conversation = f"conv_{uuid.uuid4().hex[:10]}"
    artefact = _creer(sessions, racine, workspace=espace, conversation=conversation)
    texte = "Texte suffisamment long pour être indexé et produire un extrait. " * 5
    monkeypatch.setattr(
        attachment_ingest, "extraire_texte", lambda *a, **k: (texte, "native")
    )

    attachment_ingest._ingérer_un(artefact, modeles_autorises=True)

    assert evenements[0][1]["workspace_id"] == espace
    assert evenements[1][1]["workspace_id"] == espace


def test_un_claim_deja_tenu_n_emet_aucun_evenement(base, evenements):
    sessions, racine = base
    espace = _espace(sessions)
    artefact = _creer(sessions, racine, workspace=espace)
    from backend.ingestion import bail

    with sessions() as db:
        assert bail.acquérir_bail(db, artefact, "autre-proprietaire") is not None

    attachment_ingest._ingérer_un(artefact, modeles_autorises=True)

    assert evenements == []


def test_une_perte_de_bail_n_emet_aucun_evenement(base, evenements, monkeypatch):
    sessions, racine = base
    espace = _espace(sessions)
    artefact = _creer(sessions, racine, workspace=espace)

    def bail_perdu(*args, **kwargs):
        raise BailPerdu(artefact, "jeton")

    # La perte survient AVANT le démarrage annoncé : le worker n'a pas travaillé,
    # et l'événement de démarrage ne doit pas être émis.
    monkeypatch.setattr(attachment_ingest, "_battre", bail_perdu)

    attachment_ingest._ingérer_un(artefact, modeles_autorises=True)

    assert evenements == []


def test_une_ecriture_d_etat_ratee_n_annonce_ni_started_ni_terminal(
    base, evenements, monkeypatch
):
    """Aucun événement ne peut porter un état que la base n'a pas.

    `_marquer` rend `""` quand l'écriture échoue (verrou SQLite, artefact
    disparu). L'ingestion continue — le travail d'extraction n'a pas à lever
    pour un relevé raté — mais elle se tait : annoncer `en_cours` puis `extrait`
    sur une base restée `depose` était précisément le mensonge corrigé.
    """
    sessions, racine = base
    espace = _espace(sessions)
    artefact = _creer(sessions, racine, workspace=espace)
    texte = (
        "Compte rendu de la réunion, décisions prises et responsables nommés, "
        "avec un contenu suffisamment long pour être indexé. " * 4
    )
    monkeypatch.setattr(
        attachment_ingest, "extraire_texte", lambda *a, **k: (texte, "native")
    )
    monkeypatch.setattr(attachment_ingest, "_marquer", lambda *a, **k: "")

    attachment_ingest._ingérer_un(artefact, modeles_autorises=True)

    assert evenements == []
    # La preuve que rien n'a été écrit : l'état de départ est intact.
    _, lu = _etats(sessions, artefact)
    assert lu.etat == ETAT.ETAT_DEPOSE


def test_marquer_reessaie_une_fois_puis_rend_l_etat_ecrit(base, monkeypatch):
    """Une écriture ratée est retentée UNE fois dans une nouvelle session.

    Le premier échec ne doit pas laisser l'artefact en `en_cours` alors qu'une
    seconde session courte peut encore écrire l'état final.
    """
    sessions, racine = base
    espace = _espace(sessions)
    artefact = _creer(sessions, racine, workspace=espace)
    appels: list[str] = []
    vraie = ETAT.marquer_une_fois

    def _flaky(session, artifact_id, etat, raison="", *, claim_token=None):
        appels.append(etat)
        if len(appels) == 1:
            return ""
        return vraie(session, artifact_id, etat, raison, claim_token=claim_token)

    monkeypatch.setattr(ETAT, "marquer_une_fois", _flaky)

    ecrit = attachment_ingest._marquer(artefact, ETAT.ETAT_EXTRAIT, "texte indexé")

    assert ecrit == ETAT.ETAT_EXTRAIT
    assert appels == [ETAT.ETAT_EXTRAIT, ETAT.ETAT_EXTRAIT], (
        "exactement un réessai, pas une boucle"
    )
    _, lu = _etats(sessions, artefact)
    assert lu.etat == ETAT.ETAT_EXTRAIT


def test_marquer_echec_persistant_nomme_l_artefact_et_rend_vide(
    base, monkeypatch, caplog
):
    """Un second échec est journalisé et ne prétend jamais avoir écrit.

    Rendre l'état demandé ferait émettre un terminal que la base ne porte pas :
    `_marquer` rend `""`, et l'artefact reste dans son état précédent.
    """
    sessions, racine = base
    espace = _espace(sessions)
    artefact = _creer(sessions, racine, workspace=espace)
    appels: list[int] = []

    def _toujours_en_echec(*args, **kwargs):
        appels.append(1)
        return ""

    monkeypatch.setattr(ETAT, "marquer_une_fois", _toujours_en_echec)

    with caplog.at_level("ERROR"):
        ecrit = attachment_ingest._marquer(artefact, ETAT.ETAT_EXTRAIT, "texte indexé")

    assert ecrit == ""
    assert len(appels) == 2, "un seul réessai après l'échec initial"
    assert any(
        artefact in record.message and ETAT.ETAT_EXTRAIT in record.message
        for record in caplog.records
    ), "le journal doit nommer l'artefact et l'état visé"
    _, lu = _etats(sessions, artefact)
    assert lu.etat == ETAT.ETAT_DEPOSE


def test_started_lit_en_cours_le_claim_et_le_battement_dans_la_base(base, monkeypatch):
    """Le démarrage est annoncé APRÈS l'écriture, sur ce que la base porte.

    Le faux `emit` relit la base AU MOMENT de l'appel : un événement construit
    avant l'écriture, ou sur l'état demandé plutôt que sur l'état écrit, échoue
    ici. Le claim et le battement doivent déjà être renseignés.
    """
    sessions, racine = base
    espace = _espace(sessions)
    artefact = _creer(sessions, racine, workspace=espace)
    texte = (
        "Compte rendu de la réunion, décisions prises et responsables nommés, "
        "avec un contenu suffisamment long pour être indexé. " * 4
    )
    monkeypatch.setattr(
        attachment_ingest, "extraire_texte", lambda *a, **k: (texte, "native")
    )

    from backend.inference import ipc

    demarrages: list[tuple[object, object, object]] = []

    def faux_emit(event, payload, **kwargs):
        if event == "artifact_ingestion_started":
            with sessions() as db:
                lu = db.query(Artifact).filter(Artifact.id == artefact).one()
                demarrages.append(
                    (
                        lu.ingest_state,
                        lu.ingest_claim_token,
                        lu.ingest_heartbeat_at,
                    )
                )
        return True

    monkeypatch.setattr(ipc, "emit", faux_emit)

    attachment_ingest._ingérer_un(artefact, modeles_autorises=True)

    assert len(demarrages) == 1
    etat_lu, jeton, battement = demarrages[0]
    assert etat_lu == ETAT.ETAT_EN_COURS
    assert jeton
    assert battement is not None


def test_une_perte_de_bail_apres_started_n_emet_aucun_terminal(
    base, evenements, monkeypatch
):
    """Le démarrage a eu lieu ; la perte survient PENDANT l'extraction.

    `BailPerdu` doit arrêter le worker sans aucun terminal : le nouveau
    propriétaire est seul responsable de l'état, et un `failed` émis ici
    écraserait sa tentative aux yeux de l'écran.
    """
    sessions, racine = base
    espace = _espace(sessions)
    artefact = _creer(sessions, racine, workspace=espace)

    def bail_perdu(*args, **kwargs):
        raise BailPerdu(artefact, "jeton")

    monkeypatch.setattr(attachment_ingest, "extraire_texte", bail_perdu)

    attachment_ingest._ingérer_un(artefact, modeles_autorises=True)

    assert [nom for nom, _, _ in evenements] == ["artifact_ingestion_started"]


def test_une_perte_de_bail_a_la_persistance_retourne_sans_terminal(
    base, evenements, monkeypatch
):
    """`BailPerdu` levé par la persistance est une sortie propre, pas un crash.

    Le worker s'arrête sans observation ni état terminal : le repreneur est seul
    responsable, comme pour une perte survenue pendant l'extraction.
    """
    sessions, racine = base
    espace = _espace(sessions)
    artefact = _creer(sessions, racine, workspace=espace)

    def bail_perdu(*args, **kwargs):
        raise BailPerdu(artefact, "jeton")

    monkeypatch.setattr(attachment_ingest, "_persister_extraction_complete", bail_perdu)

    attachment_ingest._ingérer_un(artefact, modeles_autorises=True)

    assert [nom for nom, _, _ in evenements] == ["artifact_ingestion_started"]
    with sessions() as db:
        lu = db.query(Artifact).filter(Artifact.id == artefact).one()
        assert lu.ingest_state == ETAT.ETAT_EN_COURS


def test_un_echec_apres_un_texte_extrait_annonce_l_etat_restaure(
    base, evenements, monkeypatch
):
    """Un échec APRÈS un texte déjà extrait restaure `extrait`, et le dit.

    La base ne porte pas `echec` : `etat.marquer` a restauré le texte antérieur.
    L'événement terminal doit suivre cet état réel (`finished`), sinon l'écran
    afficherait un échec que le dépôt ne connaît pas.
    """
    sessions, racine = base
    espace = _espace(sessions)
    artefact = _creer(sessions, racine, etat=ETAT.ETAT_EXTRAIT, workspace=espace)

    def panne(*args, **kwargs):
        raise RuntimeError("lecteur cassé")

    monkeypatch.setattr(attachment_ingest, "extraire_texte", panne)

    attachment_ingest._ingérer_un(artefact, modeles_autorises=True)

    assert [nom for nom, _, _ in evenements] == [
        "artifact_ingestion_started",
        "artifact_ingestion_finished",
    ]
    assert evenements[-1][1]["state"] == ETAT.ETAT_EXTRAIT
    _, lu = _etats(sessions, artefact)
    assert lu.etat == ETAT.ETAT_EXTRAIT


def test_une_bascule_au_plafond_apres_texte_extrait_annonce_l_etat_restaure(
    base, evenements
):
    """`_basculer_reprise` rend l'état RESTAURÉ, et l'issue suit cet état.

    Au plafond, la dette `depose` devient `echec` ; mais un texte déjà extrait
    est restauré par `etat.marquer`. Le terminal doit donc être `finished` sur
    `extrait` — pas `failed` sur un `echec` que la base ne porte pas.
    """
    sessions, racine = base
    espace = _espace(sessions)
    artefact = _creer(sessions, racine, etat=ETAT.ETAT_EXTRAIT, workspace=espace)

    # Le démarrage mémorise `extrait` comme état à restaurer.
    assert (
        attachment_ingest._marquer(artefact, ETAT.ETAT_EN_COURS, "extraction demandée")
        == ETAT.ETAT_EN_COURS
    )

    etat_ecrit, motif = attachment_ingest._basculer_reprise(
        artefact,
        ETAT.ETAT_DEPOSE,
        "extraction impossible",
        ETAT.PLAFOND_ESSAIS,
    )
    assert etat_ecrit == ETAT.ETAT_EXTRAIT
    attachment_ingest._emettre_issue(artefact, etat_ecrit, motif, "text")

    assert [nom for nom, _, _ in evenements] == ["artifact_ingestion_finished"]
    assert evenements[0][1]["state"] == ETAT.ETAT_EXTRAIT
    _, lu = _etats(sessions, artefact)
    assert lu.etat == ETAT.ETAT_EXTRAIT


def test_une_bascule_de_reprise_non_ecrite_n_emet_aucun_terminal(
    base, evenements, monkeypatch
):
    """Quand `_basculer_reprise` n'a rien pu écrire, l'ingestion se tait.

    C'est le chemin `ModeleRequis` : la dette reste à reprendre, mais aucun
    terminal ne doit annoncer un `depose` que la base ne porte pas. L'appel ne
    lève pas pour autant — l'absence d'état est journalisée, pas fatale.
    """
    sessions, racine = base
    espace = _espace(sessions)
    artefact = _creer(sessions, racine, workspace=espace)

    def modele_requis(*args, **kwargs):
        raise attachment_ingest.ModeleRequis("PDF sans couche texte")

    monkeypatch.setattr(attachment_ingest, "_extraire_sans_modele", modele_requis)
    monkeypatch.setattr(attachment_ingest, "_marquer", lambda *a, **k: "")

    attachment_ingest._ingérer_un(artefact, modeles_autorises=False)

    assert evenements == []


def test_une_observation_non_ecrite_emet_un_terminal_coherent(
    base, evenements, monkeypatch
):
    """Une observation impossible à persister reste une issue, pas un silence.

    L'état `echec` est écrit avant la remontée ; l'événement est choisi sur cet
    état réel. Sans cela, l'écran attendrait indéfiniment une fin d'ingestion.
    """
    sessions, racine = base
    espace = _espace(sessions)
    artefact = _creer(sessions, racine, workspace=espace)
    texte = (
        "Compte rendu de la réunion, décisions prises et responsables nommés, "
        "avec un contenu suffisamment long pour être indexé. " * 4
    )
    monkeypatch.setattr(
        attachment_ingest, "extraire_texte", lambda *a, **k: (texte, "native")
    )

    from backend.search import observations

    def echec(*args, **kwargs):
        raise observations.ObservationNonÉcrite("disque plein")

    monkeypatch.setattr(observations, "record_extraction_observation", echec)

    with pytest.raises(observations.ObservationNonÉcrite):
        attachment_ingest._ingérer_un(artefact, modeles_autorises=True)

    assert [nom for nom, _, _ in evenements] == [
        "artifact_ingestion_started",
        "artifact_ingestion_failed",
    ]
    assert evenements[-1][1]["state"] == ETAT.ETAT_ECHEC


def test_un_doublon_d_admission_rend_une_liste_vide_sans_evenement(
    base, evenements, monkeypatch
):
    """`lancer_ingestion` ne prétend pas relancer un artefact déjà enfilé.

    Aucun événement n'est émis à l'admission : l'écran ne doit pas recevoir un
    démarrage pour un travail qui n'a pas été créé.
    """
    sessions, racine = base
    espace = _espace(sessions)
    artefact = _creer(sessions, racine, workspace=espace)

    import queue

    monkeypatch.setattr(attachment_ingest, "_en_file", set())
    monkeypatch.setattr(attachment_ingest, "_file_admission", queue.Queue())
    monkeypatch.setattr(
        attachment_ingest, "_demarrer_worker_si_necessaire", lambda: None
    )

    assert _LANCER_INGESTION_ORIGINAL([artefact]) == [artefact]
    assert _LANCER_INGESTION_ORIGINAL([artefact]) == []
    assert evenements == []


# ---------------------------------------------------------------------------
# 6.3 — commande de relance
# ---------------------------------------------------------------------------


def _lien_workspace(sessions, artifact_id: str, workspace: str) -> None:
    with sessions() as db:
        db.add(
            ArtifactLink(
                id=f"lnk_{uuid.uuid4().hex[:12]}",
                artifact_id=artifact_id,
                scope_kind="workspace",
                scope_id=workspace,
                role="shared",
            )
        )
        db.commit()


def _retry(sessions, artifact_id: str, **extras):
    from backend.ipc import attachments as A

    with sessions() as db:
        return A.artifact_ingestion_retry(db, {"artifactId": artifact_id, **extras})


@pytest.mark.parametrize(
    "etat",
    [ETAT.ETAT_ECHEC, ETAT.ETAT_DEPOSE, ETAT.ETAT_PARTIEL, ETAT.ETAT_TRONQUE],
)
def test_la_relance_est_acceptee_pour_une_dette(base, monkeypatch, etat):
    sessions, racine = base
    espace = _espace(sessions)
    artefact = _creer(sessions, racine, etat=etat, workspace=espace)
    appels: list[tuple[list[str], bool]] = []

    def admettre(ids, *, modeles_autorises=True):
        appels.append((list(ids), modeles_autorises))
        return list(ids)

    monkeypatch.setattr(attachment_ingest, "lancer_ingestion", admettre)
    monkeypatch.setattr(attachment_ingest, "execution_en_cours", lambda _id: False)

    reponse = _retry(sessions, artefact)

    assert reponse["accepted"] is True
    assert reponse["state"] == etat
    assert appels == [([artefact], True)]


@pytest.mark.parametrize(
    "etat",
    [
        ETAT.ETAT_EXTRAIT,
        ETAT.ETAT_VIDE,
        ETAT.ETAT_EN_COURS,
        ETAT.ETAT_BINAIRE,
        ETAT.ETAT_INCONNU,
    ],
)
def test_la_relance_est_refusee_pour_un_verdict_ou_un_travail(base, monkeypatch, etat):
    sessions, racine = base
    espace = _espace(sessions)
    artefact = _creer(sessions, racine, etat=etat, workspace=espace)
    appels: list[list[str]] = []
    monkeypatch.setattr(
        attachment_ingest,
        "lancer_ingestion",
        lambda ids, **kw: appels.append(list(ids)) or list(ids),
    )

    reponse = _retry(sessions, artefact)

    assert reponse["accepted"] is False
    assert reponse["state"] == etat
    assert reponse["reason"]
    assert appels == []


def test_un_doublon_en_file_ne_se_pretend_pas_relance(base, monkeypatch):
    sessions, racine = base
    espace = _espace(sessions)
    artefact = _creer(sessions, racine, etat=ETAT.ETAT_ECHEC, workspace=espace)
    appels: list[list[str]] = []
    monkeypatch.setattr(attachment_ingest, "execution_en_cours", lambda _id: True)
    monkeypatch.setattr(
        attachment_ingest,
        "lancer_ingestion",
        lambda ids, **kw: appels.append(list(ids)) or list(ids),
    )

    reponse = _retry(sessions, artefact)

    assert reponse["accepted"] is False
    assert "file" in reponse["reason"] or "cours" in reponse["reason"]
    assert appels == []


def test_un_artefact_supprime_est_refuse(base):
    sessions, racine = base
    espace = _espace(sessions)
    artefact = _creer(
        sessions, racine, etat=ETAT.ETAT_ECHEC, workspace=espace, supprime=True
    )

    with pytest.raises(LookupError) as refus:
        _retry(sessions, artefact)

    assert "ARTIFACT_NOT_FOUND" in str(refus.value)


def test_un_lien_retire_est_refuse(base):
    sessions, racine = base
    espace = _espace(sessions)
    artefact = _creer(sessions, racine, etat=ETAT.ETAT_ECHEC, workspace=espace)
    with sessions() as db:
        db.query(ArtifactLink).filter(ArtifactLink.artifact_id == artefact).update(
            {"removed_at": ETAT.utc_naif()}, synchronize_session=False
        )
        db.commit()

    with pytest.raises(LookupError) as refus:
        _retry(sessions, artefact)

    assert "LINK_NOT_FOUND" in str(refus.value)


def test_un_espace_en_corbeille_est_refuse(base):
    sessions, racine = base
    espace = _espace(sessions)
    artefact = _creer(sessions, racine, etat=ETAT.ETAT_ECHEC, workspace=espace)
    with sessions() as db:
        db.query(Workspace).filter(Workspace.id == espace).update(
            {"state": "trashed"}, synchronize_session=False
        )
        db.commit()

    with pytest.raises(LookupError) as refus:
        _retry(sessions, artefact)

    assert "WORKSPACE_NOT_FOUND" in str(refus.value)


# ---------------------------------------------------------------------------
# 6.4 — « réparable » vs « à réessayer » dans le rapport d'écran
# ---------------------------------------------------------------------------


def test_resume_pour_ecran_separe_les_echecs_relancables(
    base_depot,  # noqa: F811 — fixture importée de test_depot_integrite
):
    from test_depot_integrite import _espace, _verser

    from backend.services import depot_integrite as integrite

    _, sessions = base_depot
    db = sessions()
    try:
        espace = _espace(db, "Espace à réessayer")
        ids = _verser(
            db,
            {"workspaceId": espace},
            ["echec.pdf", "vide.pdf"],
            ["a", "b"],
            "reessai",
        )
        db.query(Artifact).filter(Artifact.id == ids[0]).update(
            {
                "ingest_state": ETAT.ETAT_ECHEC,
                "ingest_reason": "extraction impossible — lecteur cassé",
            },
            synchronize_session=False,
        )
        db.query(Artifact).filter(Artifact.id == ids[1]).update(
            {
                "ingest_state": ETAT.ETAT_VIDE,
                "ingest_reason": "aucun texte reconnu",
            },
            synchronize_session=False,
        )
        db.commit()

        rapport = integrite.resume_pour_ecran(db, integrite.verifier_depot(db))

        assert rapport["a_reessayer"] == 1
        espace_rapport = rapport["espaces"][0]
        assert espace_rapport["a_reessayer"] == 1
        # Le `vide` reste un refus, pas une promesse de relance utile.
        assert espace_rapport["refuses_par_type"].get("extraction_absente") == 1
        etats = {
            exemple["display_name"]: exemple["ingest_state"]
            for exemple in espace_rapport["exemples"]
            if exemple["type"] == "extraction_absente"
        }
        assert set(etats.values()) == {ETAT.ETAT_ECHEC, ETAT.ETAT_VIDE}
    finally:
        db.close()


def test_le_client_ne_choisit_ni_chemin_ni_modele(base, monkeypatch):
    sessions, racine = base
    espace = _espace(sessions)
    artefact = _creer(sessions, racine, etat=ETAT.ETAT_ECHEC, workspace=espace)
    appels: list[tuple[list[str], bool]] = []

    def admettre(ids, *, modeles_autorises=True):
        appels.append((list(ids), modeles_autorises))
        return list(ids)

    monkeypatch.setattr(attachment_ingest, "lancer_ingestion", admettre)
    monkeypatch.setattr(attachment_ingest, "execution_en_cours", lambda _id: False)

    reponse = _retry(
        sessions,
        artefact,
        chemin="/tmp/ailleurs.bin",
        modele="un-modele-choisi-par-le-client",
        modeles_autorises=False,
    )

    assert reponse["accepted"] is True
    assert appels == [([artefact], True)]
