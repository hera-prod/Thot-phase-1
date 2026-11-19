"""L'intégrité du dépôt : un fichier versé doit être trouvable, et rien ne doit être perdu.

Deux défauts mesurés sur le projet partagent un même module de maintenance. Le premier :
trois fichiers du dépôt sur quatre étaient INVISIBLES à la recherche textuelle, parce
que `cards.ensure_card_on_import` indexe une ligne par lien actif AU MOMENT DE L'APPEL —
un dépôt versé plus tard n'en reçoit jamais. Le second est la conséquence qu'il faut
empêcher d'arriver : une maintenance qui « répare » un dépôt en effaçant ce qu'elle ne
comprend pas.

Chaque test verrouille donc un pan de ce contrat, et le test central (`..._par_mot_cle`)
est écrit pour ÉCHOUER si la réparation disparaît : c'est lui qui prouve que le module
sert à quelque chose, et non qu'il produit un rapport bien formé.

Montage repris de `test_depot_espace.py` : base neuve, `SessionLocal` du projet
redirigée, racines de données jetables, ingestion neutralisée. Les fichiers entrent par
le VRAI chemin de dépôt (staging puis `attachments_prepare`) — un artefact fabriqué à la
main ne prouverait rien du contrat de dépôt. Aucun octet ni aucune ligne du projet
n'est touché.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import contextmanager
from datetime import datetime

import pytest
from sqlalchemy import create_engine, event, text as sql_text
from sqlalchemy.orm import sessionmaker

from backend.config.paths import get_data_dir
from backend.db import database as database_module
from backend.db.models import (
    Artifact,
    ArtifactCard,
    ArtifactLink,
    Base,
    Blob,
    Conversation,
    DocRef,
)
from backend.ingestion import attachment_ingest
from backend.ipc import attachments as A
from backend.search import fts as Fts
from backend.services import depot_integrite as integrite
from backend.services import espaces as service
from backend.tools.base import ToolContext
from backend.tools.find_files import FindFilesTool

# Un appel de dépôt accepte huit pièces : au-delà, il faut lotir.
_LOTS = 8


def pdf_bytes(marqueur: str = "rapport") -> bytes:
    """PDF minimal mais structurallement valide (en-tête + catalogue + EOF)."""
    return (
        b"%PDF-1.7\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 595 842]>>endobj\n"
        b"trailer<</Root 1 0 R>>\n"
        b"%%EOF " + marqueur.encode()
    )


@pytest.fixture()
def base_depot(tmp_path, monkeypatch):
    """Base neuve et jetable, index FTS présent, `SessionLocal` redirigée, racines
    de données jetables, ingestion neutralisée, moteur rendu pour compter les requêtes."""
    engine = create_engine(f"sqlite:///{tmp_path / 'depot-integrite.db'}")
    Base.metadata.create_all(bind=engine)
    from backend.db.database import _ensure_fts

    _ensure_fts(engine)  # sinon la fiche n'a nulle part où s'indexer
    sessions = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    monkeypatch.setattr(database_module, "SessionLocal", sessions)
    monkeypatch.setenv("THOT_PORTABLE", "1")
    monkeypatch.setenv("THOT_APP_DATA_DIR", str(tmp_path / "app"))
    monkeypatch.setattr(
        attachment_ingest, "lancer_ingestion", lambda artifact_ids: None
    )
    return engine, sessions


@contextmanager
def _session(sessions):
    db = sessions()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def _requetes(engine):
    """Trace les instructions SQL réellement envoyées au moteur pendant le bloc."""
    vues: list[str] = []

    def _avant(conn, cursor, statement, parameters, context, executemany):  # noqa: ARG001
        vues.append(statement)

    event.listen(engine, "before_cursor_execute", _avant)
    try:
        yield vues
    finally:
        event.remove(engine, "before_cursor_execute", _avant)


# ---------- Montage : dépôt réel, fiches indexées comme après ingestion ----------


def _espace(db, nom: str) -> str:
    return str(service.create_workspace(db, nom).id)


def _conversation(db, conv_id: str, workspace_id: str | None) -> str:
    db.add(
        Conversation(
            id=conv_id, title="Discussion", mode="chat", workspace_id=workspace_id
        )
    )
    db.commit()
    return conv_id


def _verser(
    db, dest: dict, noms: list[str], prefixes: list[str], request_id: str
) -> list[str]:
    """Promeut des fichiers stagés vers une discussion OU vers le dépôt d'un espace."""
    ids: list[str] = []
    jetons = [
        A.stage_picker_bytes(
            db,
            {
                "request_id": f"{request_id}-{index}",
                "filename": nom,
                "bytes": pdf_bytes(prefixe),
            },
        )["items"][0]["stage_item_id"]
        for index, (nom, prefixe) in enumerate(zip(noms, prefixes))
    ]
    for debut in range(0, len(jetons), _LOTS):
        tranche = jetons[debut : debut + _LOTS]
        ids.extend(
            A.attachments_prepare(
                db,
                {
                    **dest,
                    "stageItemIds": tranche,
                    "names": noms[debut : debut + _LOTS],
                },
            )["artifact_ids"]
        )
    return ids


def _fiche(db, artifact_id: str) -> None:
    """Fiche minimale + ligne d'index par lien actif : ce que l'ingestion fait après
    la promotion.

    L'ingestion est neutralisée dans cette suite : sans elle, AUCUN fichier du dépôt
    n'aurait de fiche, donc aucune ligne d'index — on testerait l'absence d'analyse,
    pas l'intégrité du dépôt. La seule exception est le test qui fabrique volontairement
    l'anomalie : là, on n'appelle PAS cette fonction.
    """
    from backend.search import cards as Cards

    artefact = db.query(Artifact).filter(Artifact.id == artifact_id).one()
    Cards.ensure_card_on_import(db, artefact)
    db.commit()


def _depot(
    db,
    workspace_id: str,
    noms: list[str],
    prefixes: list[str],
    request_id: str = "depot-integrite",
) -> list[str]:
    """Dépôt SAIN : versé, indexé. C'est la référence contre laquelle on mesure."""
    ids = _verser(db, {"workspaceId": workspace_id}, noms, prefixes, request_id)
    for artifact_id in ids:
        _fiche(db, artifact_id)
    return ids


def _verser_puis_depot(
    db,
    workspace_id: str,
    conversation_id: str,
    nom: str,
    prefixe: str,
    request_id: str = "depot-tardif",
) -> str:
    """Reproduit l'anomalie MESURÉE : fiche créée à l'import en discussion, dépôt de
    l'espace partagé PLUS TARD. Le lien de dépôt ne reçoit alors aucune ligne d'index,
    et c'est le fichier qui devient introuvable par mot-clé."""
    (artifact_id,) = _verser(
        db, {"conversationId": conversation_id}, [nom], [prefixe], request_id
    )
    _fiche(db, artifact_id)
    partage = service.share_artifact(
        db, workspace_id, artifact_id, from_conv_id=conversation_id
    )
    assert partage["created"] is True and partage["bytes_copied"] == 0
    return artifact_id


def _appeler_find_files(sessions, conversation_id: str, arguments: dict) -> dict:
    ctx = ToolContext(run_id="r1", request_id=conversation_id, conv_id=conversation_id)
    resultat = asyncio.run(FindFilesTool().call(arguments, ctx))
    assert resultat.ok, resultat.display_summary
    return json.loads(resultat.content)


def _etat(sessions) -> dict:
    """État de la base, comparé par CONTENU et non par nombre brut.

    Cinq tables sont comptées ligne à ligne : une ligne perdue s'y verrait. Pour
    `artifact_search`, le NOMBRE ne veut rien dire — `index_card_row` RÉÉCRIT une ligne
    (il l'efface puis la réinsère) et une réparation doit en AJOUTER. On compare donc
    les COUPLES (artefact, portée) présents : c'est ce que la recherche interroge.
    """
    db = sessions()
    try:
        couples = sorted(
            tuple(row)
            for row in db.execute(
                sql_text(
                    "SELECT artifact_id, scope_kind, scope_id FROM artifact_search"
                )
            )
        )
        return {
            "artifacts": db.query(Artifact).count(),
            "artifact_links": db.query(ArtifactLink).count(),
            "blobs": db.query(Blob).count(),
            "doc_refs": db.query(DocRef).count(),
            "artifact_cards": db.query(ArtifactCard).count(),
            "lignes_index": db.execute(
                sql_text("SELECT COUNT(*) FROM artifact_search")
            ).scalar(),
            "couples_indexes": couples,
            "noms": {a.id: a.display_name for a in db.query(Artifact).all()},
        }
    finally:
        db.close()


# ---------- 1. Un dépôt sain n'a rien à signaler ----------


def test_un_depot_sain_ne_signale_aucune_anomalie(base_depot):
    """Un dépôt versé puis indexé est SAIN — des deux côtés.

    Le test d'abord du vide, volontairement : une anomalie détectée à tort ferait
    détruire une correction (`nom_vide` serait « réparé » sur un fichier bien nommé), et
    une réparation sur un dépôt sain doit être un no-op total.
    """
    _, sessions = base_depot
    with _session(sessions) as db:
        espace = _espace(db, "Veille")
        deposes = _depot(
            db,
            espace,
            ["Rapport_trimestriel.pdf", "Budget_T3.pdf", "Notes_reunion.pdf"],
            ["rapport-veille", "budget-veille", "notes-veille"],
        )

    with _session(sessions) as db:
        rapport = integrite.verifier_depot(db, espace)

    assert rapport["ok"] is True and rapport["sain"] is True
    assert rapport["diagnostics"] == []
    assert set(rapport["anomalies"]) == set(integrite.TYPES_ANOMALIES)
    assert rapport["par_type"] == {nom: 0 for nom in integrite.TYPES_ANOMALIES}
    assert rapport["mesure"] == {
        "liens_actifs": 3,
        "artefacts_vivants": 3,
        "fiches_actives": 3,
        "paires_indexees": 3,
    }

    with _session(sessions) as db:
        reparation = integrite.reparer_depot(db, espace)

    assert reparation["repares_effectuees"] == {"index_manquant": 0, "nom_vide": 0}
    assert reparation["signales"] == {
        nom: []
        for nom in integrite.TYPES_ANOMALIES
        if nom not in integrite.TYPES_REPARABLES
    }
    assert reparation["non_repares"] == []
    assert reparation["apres"]["sain"] is True
    assert sorted(_etat(sessions)["noms"]) == sorted(deposes)


# ---------- 2. Le fichier sans ligne d'index redevient trouvable PAR MOT-CLÉ ----------


def test_un_fichier_sans_ligne_d_index_est_signale_puis_retrouve_par_mot_cle(
    base_depot,
):
    """La preuve que la réparation sert à quelque chose : `find_files` le trouve.

    Séquence reproduite depuis la mesure — fichier joint à une discussion, indexé le
    17/09, puis versé au dépôt de l'espace le 30/09. Le fichier est alors VISIBLE à
    l'inventaire du dépôt et INVISIBLE par mot-clé : c'est bien le couple
    (workspace, artefact) qui manque dans l'index, et c'est ce couple-là que la
    réparation doit écrire.

    La discussion qui cherche n'est PAS celle qui a joint le fichier — sinon elle
    retrouverait sa propre ligne d'index, sous sa propre portée, et le test ne
    prouverait rien. C'est une autre discussion du même espace : elle voit le dépôt,
    elle interroge la portée `workspace`, et elle ne trouve rien. Le test échouerait si
    la réparation disparaissait, ou si elle réécrivait l'index sous une autre portée.
    """
    _, sessions = base_depot
    with _session(sessions) as db:
        espace = _espace(db, "Finance")
        source = _conversation(db, "conv_source", espace)
        cherche = _conversation(db, "conv_cherche", espace)
        tardif = _verser_puis_depot(
            db, espace, source, "Rapport_trimestriel.pdf", "rapport-tardif"
        )

        # Fait brut : une seule ligne d'index, portée par la discussion d'origine.
        lignes = db.execute(
            sql_text(
                "SELECT scope_kind, scope_id FROM artifact_search WHERE artifact_id = :a"
            ),
            {"a": tardif},
        ).all()
        assert [tuple(l) for l in lignes] == [("conversation", source)]
        # L'inventaire, lui, montre le fichier : c'est bien la recherche qui le perd.
        assert [f["artifact_id"] for f in service.depot(db, espace)["files"]] == [
            tardif
        ]

    avant = _appeler_find_files(sessions, cherche, {"query": "trimestriel"})
    assert avant["ok"] is True and avant["results"] == []

    with _session(sessions) as db:
        rapport = integrite.verifier_depot(db, espace)

    assert rapport["par_type"]["index_manquant"] == 1
    (manquante,) = rapport["anomalies"]["index_manquant"]
    assert manquante["artifact_id"] == tardif
    assert (manquante["scope_kind"], manquante["scope_id"]) == ("workspace", espace)
    assert manquante["workspace_id"] == espace
    assert (
        manquante["fiche_revision"] == 1
    )  # la fiche existe : c'est l'INDEX qui manque

    with _session(sessions) as db:
        reparation = integrite.reparer_depot(db, espace)

    assert reparation["repares_effectuees"]["index_manquant"] == 1
    assert (
        reparation["repares"]["index_manquant"][0]["indexee"] == f"workspace:{espace}"
    )
    assert reparation["apres"]["sain"] is True

    apres = _appeler_find_files(sessions, cherche, {"query": "trimestriel"})
    assert [r["name"] for r in apres["results"]] == ["Rapport_trimestriel.pdf"]
    assert apres["results"][0]["public_id"] == "doc_1"
    assert apres["results"][0]["origin"] == "workspace"


# ---------- 3. Le nom vide : signalé, réparé, et visible à l'inventaire ----------


def test_un_nom_vide_est_signale_puis_repare(base_depot):
    """Un artefact sans nom n'est même pas affiché par `depot()`, et se répare sans
    jamais inventer : le nom d'origine d'abord, l'empreinte du blob en dernier recours.

    L'empreinte est un repli, pas un beau nom — mais c'est une adresse EXACTE et stable :
    l'utilisateur retrouve son fichier, alors qu'un nom plausible serait un mensonge.
    """
    _, sessions = base_depot
    with _session(sessions) as db:
        espace = _espace(db, "Inventaire")
        (sans_nom,) = _depot(db, espace, ["Contrat_cadre.pdf"], ["contrat-nom"])
        artefact = db.query(Artifact).filter(Artifact.id == sans_nom).one()
        artefact.display_name = ""
        artefact.original_filename = ""
        db.commit()
        # Le dépôt ne montre QUE les fichiers qui portent un nom : l'anomalie est donc
        # visible par l'utilisateur, pas seulement en base.
        assert service.depot(db, espace)["files"] == []

    with _session(sessions) as db:
        rapport = integrite.verifier_depot(db, espace)

    assert rapport["par_type"]["nom_vide"] == 1
    (anonyme,) = rapport["anomalies"]["nom_vide"]
    assert anonyme["artifact_id"] == sans_nom
    assert anonyme["source_du_nom"] == "empreinte du blob"
    assert anonyme["nom_retrouve"] == db_sha(sessions, anonyme["blob_id"])

    with _session(sessions) as db:
        reparation = integrite.reparer_depot(db, espace)

    assert reparation["repares_effectuees"]["nom_vide"] == 1
    assert (
        reparation["repares"]["nom_vide"][0]["display_name"] == anonyme["nom_retrouve"]
    )
    assert reparation["apres"]["sain"] is True

    with _session(sessions) as db:
        affiche = db.query(Artifact).filter(Artifact.id == sans_nom).one().display_name
        assert affiche == anonyme["nom_retrouve"]
        assert [f["artifact_id"] for f in service.depot(db, espace)["files"]] == [
            sans_nom
        ]


def db_sha(sessions, blob_id: str) -> str:
    """L'empreinte du blob, lue par la base — la source de vérité du dernier recours."""
    db = sessions()
    try:
        return str(db.query(Blob).filter(Blob.id == blob_id).one().sha256)
    finally:
        db.close()


# ---------- 4. Réparer deux fois ne change rien ----------


def test_reparer_deux_fois_ne_change_rien(base_depot):
    """Idempotence : l'état après une réparation est IDENTIQUE à celui après deux.

    Une maintenance qui n'est pas idempotente finit par faire des dégâts au deuxième
    passage, et personne ne le voit parce que la première exécution était bonne. Le test
    compare trois états — avant, après une fois, après deux — et exige que le PREMIER
    ait changé quelque chose, sans quoi il ne prouverait rien.
    """
    _, sessions = base_depot
    avec_trois_defauts_et_ignore = avec_trois_defauts(sessions)
    absente = avec_trois_defauts_et_ignore["fichier_absent"]

    avant = _etat(sessions)
    with _session(sessions) as db:
        premiere = integrite.reparer_depot(db, None)
    apres_une = _etat(sessions)
    with _session(sessions) as db:
        seconde = integrite.reparer_depot(db, None)
    apres_deux = _etat(sessions)

    assert premiere["repares_effectuees"] == {"index_manquant": 1, "nom_vide": 1}
    assert seconde["repares_effectuees"] == {"index_manquant": 0, "nom_vide": 0}
    assert seconde["repares"] == {"index_manquant": [], "nom_vide": []}
    assert apres_une == apres_deux
    # Sans cette ligne, le test passerait sur une réparation qui ne fait rien.
    assert avant["couples_indexes"] != apres_une["couples_indexes"]
    # Ce qui n'est pas réparable reste signalé à l'identique : le refus est stable.
    attendus = {("fichier_absent", absente)}
    assert _types(seconde["non_repares"]) == _types(premiere["non_repares"]) == attendus
    assert seconde["apres"]["par_type"]["index_manquant"] == 0
    assert seconde["apres"]["par_type"]["nom_vide"] == 0
    assert seconde["apres"]["par_type"]["fichier_absent"] == 1


def _types(entrees: list[dict]) -> set[tuple[str, str | None]]:
    return {(a["type"], a.get("artifact_id")) for a in entrees}


def _chemin_du_blob(db, artifact_id: str):
    """L'adresse sur le disque que la base déclare pour cet artefact."""
    blob_id = db.query(Artifact).filter(Artifact.id == artifact_id).one().blob_id
    relative_path = str(db.query(Blob).filter(Blob.id == blob_id).one().relative_path)
    return get_data_dir() / relative_path


def avec_trois_defauts(sessions) -> dict[str, str]:
    """Un dépôt portant un défaut de chaque famille : index, nom, disque.

    Les trois sont traités différemment — les deux premiers réparés, le troisième
    signalé — et les mélanger dans un seul montage prouve qu'ils n'interfèrent pas
    entre eux, au lieu d'en tester un par base jetable.
    """
    with _session(sessions) as db:
        espace = _espace(db, "Maintenance")
        conversation = _conversation(db, "conv_maintenance", espace)
        tardif = _verser_puis_depot(
            db, espace, conversation, "Contrat_cadre.pdf", "contrat-tardif"
        )
        (sans_nom,) = _depot(
            db, espace, ["Annexe_fiscale.pdf"], ["annexe-nom"], request_id="depot-nom"
        )
        artefact = db.query(Artifact).filter(Artifact.id == sans_nom).one()
        artefact.display_name = ""
        artefact.original_filename = ""
        (absent,) = _depot(
            db,
            espace,
            ["Piece_detachee.pdf"],
            ["piece-absente"],
            request_id="depot-absent",
        )
        chemin = _chemin_du_blob(db, absent)
        db.commit()
    assert chemin.is_file()
    chemin.unlink()
    return {
        "espace": espace,
        "conversation": conversation,
        "index_manquant": tardif,
        "nom_vide": sans_nom,
        "fichier_absent": absent,
    }


# ---------- 5. Aucune ligne supprimée ----------


def test_la_reparation_n_efface_aucune_ligne(base_depot):
    """Cinq tables comptées AVANT et APRÈS, égalité stricte exigée.

    `artifact_search` est le seul écart, et il est un AJOUT : `index_card_row`
    réécrit une ligne (il l'efface puis la réinsère) sur le couple qu'on réindexe, donc
    une ligne de plus par couple réparé, et surtout AUCUNE perte. On le compare donc par
    contenu — les couples (artefact, portée) présents — et on exige que ce contenu
    APRÈS contienne exactement celui d'avant, plus les nouveaux couples. C'est la seule
    comparaison honnête : compter des lignes ici mesurerait la réécriture faite par `fts`,
    pas la conservation des données.
    """
    _, sessions = base_depot
    avec_trois_defauts(sessions)

    avant = _etat(sessions)
    with _session(sessions) as db:
        reparation = integrite.reparer_depot(db, None)
    apres = _etat(sessions)

    for table in ("artifacts", "artifact_links", "blobs", "doc_refs", "artifact_cards"):
        assert apres[table] == avant[table], table
    assert apres["noms"] == {
        **avant["noms"],
        **{
            reparation["repares"]["nom_vide"][0]["artifact_id"]: reparation["repares"][
                "nom_vide"
            ][0]["display_name"]
        },
    }

    ajouts = {
        c for c in apres["couples_indexes"] if c not in set(avant["couples_indexes"])
    }
    disparus = {
        c for c in avant["couples_indexes"] if c not in set(apres["couples_indexes"])
    }
    assert disparus == set()
    assert len(ajouts) == reparation["repares_effectuees"]["index_manquant"]
    assert apres["lignes_index"] == avant["lignes_index"] + len(ajouts)


# ---------- 6 & 7. Ce qui est signalé n'est jamais réparé ----------


def test_un_fichier_absent_du_disque_est_signale_et_non_repare(base_depot):
    """Des octets disparus ne se réparent pas : se tromper de chemin serait pire.

    Le module peut-il écrire quelque part ? Non : il n'a aucun chemin de remplacement.
    La seule chose honnête à faire est de nommer le fichier attendu, pour que
    l'utilisateur sache ce qu'il doit retrouver — et c'est exactement ce que la
    maintenance refuse de faire à sa place.
    """
    _, sessions = base_depot
    with _session(sessions) as db:
        espace = _espace(db, "Disque")
        (versé,) = _depot(db, espace, ["Scan_justificatif.pdf"], ["scan-disque"])
        blob = (
            db.query(Blob)
            .filter(
                Blob.id == db.query(Artifact).filter(Artifact.id == versé).one().blob_id
            )
            .one()
        )
        chemin = get_data_dir() / str(blob.relative_path)
        db.commit()
    assert chemin.is_file()
    chemin.unlink()

    with _session(sessions) as db:
        rapport = integrite.verifier_depot(db, espace)

    assert rapport["par_type"]["fichier_absent"] == 1
    (absente,) = rapport["anomalies"]["fichier_absent"]
    assert absente["artifact_id"] == versé and absente["workspace_id"] == espace
    assert absente["relative_path"] == blob.relative_path
    assert absente["chemin_attendu"] == str(chemin)  # on dit OÙ on l'attendait
    assert absente["taille_declaree"] == len(pdf_bytes("scan-disque"))

    with _session(sessions) as db:
        reparation = integrite.reparer_depot(db, espace)

    assert reparation["repares_effectuees"] == {"index_manquant": 0, "nom_vide": 0}
    (signalee,) = reparation["signales"]["fichier_absent"]
    assert signalee["artifact_id"] == versé
    assert signalee["raison_du_refus"] == integrite.RAISONS_DE_REFUS["fichier_absent"]
    assert reparation["apres"]["par_type"]["fichier_absent"] == 1  # toujours signalé
    assert not chemin.exists()
    with _session(sessions) as db:
        assert db.query(Blob).filter(Blob.id == blob.id).count() == 1
        assert db.query(Artifact).filter(Artifact.id == versé).count() == 1


def test_un_lien_mort_est_signale_et_non_repare(base_depot):
    """Un lien vers un artefact supprimé se signale ; ni l'un ni l'autre ne bouge.

    Retirer le lien ferait disparaître une ligne du dépôt, restaurer l'artefact
    ressusciterait un fichier que l'utilisateur a supprimé. Les deux sont des décisions
    de sa part, pas de cette maintenance.

    On vérifie aussi que l'index n'a pas bougé : la fiche du fichier supprimé existe
    déjà (il était.indexé avant d'être mis à la corbeille), et le module ne doit ni la
    réécrire ni en ajouter une — remettre en index un artefact supprimé le rendrait
    visible à la recherche alors qu'il est en corbeille.
    """
    _, sessions = base_depot
    with _session(sessions) as db:
        espace = _espace(db, "Corbeille")
        (abandon,) = _depot(db, espace, ["Ancien_contrat.pdf"], ["ancien-corbeille"])
        avant_index = _lignes_index(db, abandon)
        db.query(Artifact).filter(Artifact.id == abandon).update(
            {Artifact.deleted_at: datetime(2026, 9, 30, 12, 0, 0)},
            synchronize_session=False,
        )
        db.commit()
    assert len(avant_index) == 1

    with _session(sessions) as db:
        rapport = integrite.verifier_depot(db, espace)

    assert rapport["par_type"]["lien_mort"] == 1
    (mort,) = rapport["anomalies"]["lien_mort"]
    assert mort["artifact_id"] == abandon and mort["workspace_id"] == espace
    assert "supprim" in mort["raison"]
    assert (
        rapport["par_type"]["index_manquant"] == 0
    )  # un artefact supprimé ne se réindexe pas

    with _session(sessions) as db:
        reparation = integrite.reparer_depot(db, espace)

    assert reparation["repares_effectuees"] == {"index_manquant": 0, "nom_vide": 0}
    assert (
        reparation["signales"]["lien_mort"][0]["raison_du_refus"]
        == integrite.RAISONS_DE_REFUS["lien_mort"]
    )
    assert reparation["apres"]["par_type"]["lien_mort"] == 1
    with _session(sessions) as db:
        liens = db.query(ArtifactLink).filter(ArtifactLink.artifact_id == abandon).all()
        assert [(l.scope_kind, l.scope_id, l.removed_at) for l in liens] == [
            ("workspace", espace, None)
        ]  # le lien est toujours là
        assert db.query(Artifact).filter(Artifact.id == abandon).count() == 1
        assert _lignes_index(db, abandon) == avant_index  # l'index n'a pas bougé


def _lignes_index(db, artifact_id: str) -> list[tuple]:
    return sorted(
        tuple(row)
        for row in db.execute(
            sql_text(
                "SELECT scope_kind, scope_id, card_revision FROM artifact_search"
                " WHERE artifact_id = :a"
            ),
            {"a": artifact_id},
        )
    )


# ---------- Le coût de la vérification, et une base dégradée ----------


def test_le_cout_de_verification_ne_depend_pas_du_nombre_de_fichiers(base_depot):
    """Cinq lectures pour un dépôt d'un fichier comme pour un dépôt de vingt.

    Le N+1 est le piège naturel de ce module — un blob, une fiche, une ligne d'index par
    fichier. Il se verrait ici : un coût croissant avec la taille du dépôt. Le test
    mesure les instructions réellement envoyées au moteur, après avoir réchauffé la
    connexion pour ne pas compter l'ouverture.
    """
    engine, sessions = base_depot
    with _session(sessions) as db:  # on ouvre la connexion avant de compter
        db.execute(sql_text("SELECT 1"))

    with _session(sessions) as db:
        petit = _espace(db, "Petit")
        _depot(db, petit, ["Seul.pdf"], ["seul-petit"])
    with _requetes(engine) as vues_petit:
        with _session(sessions) as db:
            integrite.verifier_depot(db, petit)

    with _session(sessions) as db:
        grand = _espace(db, "Grand")
        _depot(
            db,
            grand,
            [f"piece{index:02d}.pdf" for index in range(20)],
            [f"piece-grand-{index}" for index in range(20)],
            request_id="depot-grand",
        )
    with _requetes(engine) as vues_grand:
        with _session(sessions) as db:
            integrite.verifier_depot(db, grand)

    assert len(vues_petit) == integrite.LECTURES_PAR_VERIFICATION
    assert len(vues_grand) == len(vues_petit) == 5
    assert all(
        "SELECT" in vue.upper() for vue in vues_grand
    )  # lecture seule, sans exception


def test_un_index_illisible_produit_un_rapport_et_non_une_exception(base_depot):
    """Base dégradée : la table d'index a disparu. Le module RAPPORTE, il ne lève pas.

    Et surtout il n'accuse pas : sans table d'index, aucun couple ne peut être déclaré
    manquant, sinon la réparation réécrirait deux cents lignes d'index pour rien. La
    source illisible part dans `diagnostics`, qui rend le dépôt NON sain alors qu'aucune
    anomalie n'est affirmée.
    """
    engine, sessions = base_depot
    with _session(sessions) as db:
        espace = _espace(db, "Degrade")
        (versé,) = _depot(db, espace, ["Facture_perdue.pdf"], ["facture-degradee"])
    with engine.begin() as conn:  # base JETABLE de la suite : ce retrait n'atteint rien
        conn.exec_driver_sql("DROP TABLE artifact_search")

    with _session(sessions) as db:
        rapport = integrite.verifier_depot(db, espace)

    assert rapport["ok"] is True and rapport["sain"] is False
    assert rapport["par_type"]["index_manquant"] == 0
    assert (
        rapport["par_type"]["fichier_absent"] == 0
    )  # les autres sources sont intactes
    assert [d for d in rapport["diagnostics"] if "artifact_search" in d]
    with _session(sessions) as db:
        reparation = integrite.reparer_depot(db, espace)
    assert reparation["repares_effectuees"] == {"index_manquant": 0, "nom_vide": 0}


# ---------- Les anomalies de stockage : signalées, jamais réécrites ----------


def test_un_blob_manquant_est_signale_et_non_repare(base_depot):
    """Un artefact sans sa ligne `blobs` : signalée, et surtout PAS recréée.

    La ligne blob porte l'empreinte et la taille des octets. La reconstituer sans les
    octets fabriquerait une preuve de contenu qui n'existe pas — exactement le genre de
    mensonge qu'une maintenance doit éviter même pour « réparer » un dépôt.
    """
    _, sessions = base_depot
    with _session(sessions) as db:
        from backend.search import cards as Cards

        espace = _espace(db, "Stockage")
        (temoignage,) = _depot(db, espace, ["Piece_muette.pdf"], ["piece-muette"])
        # Écriture en SQL brut, et c'est VOLONTAIRE : c'est le seul moyen d'obtenir un
        # artefact dont le blob_id ne désigne rien, la contrainte d'intégrité l'interdisant
        # autrement. Le moteur de la suite n'active pas `foreign_keys` — c'est exactement
        # la situation réelle qu'on veut reproduire (une écriture tierce sans la PRAGMA).
        db.execute(
            sql_text(
                "INSERT INTO artifacts (id, blob_id, kind, original_filename, display_name,"
                " created_at) VALUES ('art_orphelin', 'blob_inexistant', 'pdf', 'Fantome.pdf',"
                " 'Fantome.pdf', '2026-09-30 09:00:00')"
            )
        )
        db.execute(
            sql_text(
                "INSERT INTO artifact_links (id, artifact_id, scope_kind, scope_id, role,"
                " created_at) VALUES ('lnk_orphelin', 'art_orphelin', 'workspace', :ws,"
                " 'shared', '2026-09-30 09:00:00')"
            ),
            {"ws": espace},
        )
        db.commit()
        # Fiche et ligne d'index comprises : seul le blob manque, sinon on testerait
        # deux défauts en espérant en voir un.
        orphelin = db.query(Artifact).filter(Artifact.id == "art_orphelin").one()
        Cards.ensure_card_on_import(db, orphelin)
        Fts.index_card_row(
            db,
            orphelin,
            Cards.get_current_card(db, orphelin.id),
            "workspace",
            espace,
            "",
        )
        db.commit()

    with _session(sessions) as db:
        rapport = integrite.verifier_depot(db, espace)

    assert rapport["par_type"]["blob_manquant"] == 1
    (orphelin,) = rapport["anomalies"]["blob_manquant"]
    assert (
        orphelin["artifact_id"] == "art_orphelin"
        and orphelin["blob_id"] == "blob_inexistant"
    )
    assert orphelin["workspace_id"] == espace

    with _session(sessions) as db:
        reparation = integrite.reparer_depot(db, espace)

    assert reparation["repares_effectuees"] == {"index_manquant": 0, "nom_vide": 0}
    assert (
        reparation["signales"]["blob_manquant"][0]["raison_du_refus"]
        == integrite.RAISONS_DE_REFUS["blob_manquant"]
    )
    assert reparation["apres"]["par_type"]["blob_manquant"] == 1
    with _session(sessions) as db:
        assert db.query(Blob).filter(Blob.id == "blob_inexistant").count() == 0
        assert db.query(Artifact).filter(Artifact.id == "art_orphelin").count() == 1


def test_un_doublon_de_blob_est_signale_sans_etre_fusionne(base_depot):
    """Verser deux fois le même fichier dans un espace fait deux artefacts, un blob.

    Le dépôt affiche alors le document en double. La maintenance le SIGNALE et ne
    fusionne pas : fusionner ferait disparaître une ligne du dépôt et son `doc_refs`,
    et l'utilisateur n'a rien demandé. Le regroupement est par espace — deux espaces
    peuvent légitimement détenir le même contenu.
    """
    _, sessions = base_depot
    with _session(sessions) as db:
        espace = _espace(db, "Doublons")
        (premier, second) = _depot(
            db,
            espace,
            ["Memo_finale.pdf", "Memo_finale.pdf"],
            ["memo-doublon", "memo-doublon"],
        )

    with _session(sessions) as db:
        rapport = integrite.verifier_depot(db, espace)

    assert rapport["par_type"]["doublon"] == 2  # une entrée par artefact concerné
    couples = {
        d["artifact_id"]: d["meme_blob_que"] for d in rapport["anomalies"]["doublon"]
    }
    assert couples == {premier: [second], second: [premier]}

    with _session(sessions) as db:
        reparation = integrite.reparer_depot(db, espace)

    assert reparation["repares_effectuees"] == {"index_manquant": 0, "nom_vide": 0}
    assert len(reparation["signales"]["doublon"]) == 2
    assert reparation["apres"]["par_type"]["doublon"] == 2
    with _session(sessions) as db:
        # Un seul fichier sur le disque, deux artefacts : rien n'a été fusionné.
        assert (
            db.query(Blob)
            .filter(
                Blob.id
                == (db.query(Artifact).filter(Artifact.id == premier).one().blob_id)
            )
            .count()
            == 1
        )
        assert db.query(Artifact).count() == 2


def test_un_fichier_sur_disque_sans_ligne_blob_est_signale_et_conserve(base_depot):
    """Un `.bin` sans ligne `blobs` est un orphelin, mais la vérification ne le tue pas.

    Un tel fichier peut être une écriture atomique déjà faite dont la transaction
    n'a pas encore validé : le supprimer depuis `verifier_depot` (lecture seule)
    détruirait une écriture concurrente. On le signale, et `reparer_depot` refuse
    aussi de le supprimer pour la même raison.
    """
    _, sessions = base_depot
    data_dir = get_data_dir()
    orphelin = data_dir / "blobs" / "zz" / "sans-ligne.bin"
    orphelin.parent.mkdir(parents=True, exist_ok=True)
    orphelin.write_bytes(b"octets orphelins")

    with _session(sessions) as db:
        rapport = integrite.verifier_depot(db)

    assert rapport["par_type"]["blob_orphelin"] == 1
    (entree,) = rapport["anomalies"]["blob_orphelin"]
    assert entree["relative_path"] == "blobs/zz/sans-ligne.bin"
    assert entree["chemin"] == str(orphelin)
    assert entree["taille"] == len(b"octets orphelins")

    with _session(sessions) as db:
        reparation = integrite.reparer_depot(db)

    assert reparation["repares_effectuees"] == {"index_manquant": 0, "nom_vide": 0}
    assert (
        reparation["signales"]["blob_orphelin"][0]["raison_du_refus"]
        == (integrite.RAISONS_DE_REFUS["blob_orphelin"])
    )
    assert orphelin.is_file()
    with _session(sessions) as db:
        assert db.query(Blob).count() == 0


# ---------- 2. Récupération bornée des .bin orphelins ----------


def _orphelin(data_dir, relative: str, contenu: bytes = b"orphelin"):
    chemin = data_dir / relative
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_bytes(contenu)
    return chemin


def _vieillir(chemin, secondes: float = integrite.ORPHAN_BLOB_TTL_S + 60) -> None:
    vieux = time.time() - secondes
    os.utime(chemin, (vieux, vieux))


def test_un_bin_orphelin_expire_est_recupere_et_un_frais_conserve(base_depot):
    """Crash entre `os.replace` et `commit` : le TTL distingue expiré de frais."""
    _, sessions = base_depot
    data_dir = get_data_dir()
    expire = _orphelin(data_dir, "blobs/zz/orphelin-expire.bin")
    frais = _orphelin(data_dir, "blobs/zz/orphelin-frais.bin")
    _vieillir(expire)

    with _session(sessions) as db:
        resultat = integrite.purger_orphelins_blobs_expires(db)

    assert resultat["purged"] == 1
    assert resultat["failed"] == 0
    assert not expire.exists(), "un orphelin expiré doit être récupéré"
    assert frais.is_file(), "un orphelin frais peut être une promotion en cours"


def test_un_bin_reference_n_est_jamais_supprime_meme_expire(base_depot):
    """Une ligne `blobs` protège le fichier, quel que soit son âge."""
    _, sessions = base_depot
    data_dir = get_data_dir()
    with _session(sessions) as db:
        workspace_id = _espace(db, "Référencé")
        _depot(db, workspace_id, ["garde.pdf"], ["garde"])
        blob = db.query(Blob).one()
        chemin = data_dir / blob.relative_path
    _vieillir(chemin)

    with _session(sessions) as db:
        resultat = integrite.purger_orphelins_blobs_expires(db)

    assert resultat["purged"] == 0
    assert resultat["failed"] == 0
    assert chemin.is_file()


def test_crash_entre_commit_gc_et_unlink_est_recupere(base_depot):
    """La ligne `blobs` a été commitée supprimée, le fichier est resté."""
    _, sessions = base_depot
    data_dir = get_data_dir()
    with _session(sessions) as db:
        workspace_id = _espace(db, "Commit GC")
        _depot(db, workspace_id, ["parti.pdf"], ["parti"])
        blob = db.query(Blob).one()
        chemin = data_dir / blob.relative_path
        db.delete(blob)
        db.commit()
    _vieillir(chemin)

    with _session(sessions) as db:
        resultat = integrite.purger_orphelins_blobs_expires(db)

    assert resultat["purged"] == 1
    assert not chemin.exists()


def test_la_recuperation_ne_suit_pas_les_liens_symboliques(base_depot, tmp_path):
    """Un lien symbolique pointant hors de la racine n'est jamais suivi."""
    _, sessions = base_depot
    data_dir = get_data_dir()
    cible = tmp_path / "cible-externe.bin"
    cible.write_bytes(b"a ne pas supprimer")
    _vieillir(cible)
    lien = data_dir / "blobs" / "zz" / "lien.bin"
    lien.parent.mkdir(parents=True, exist_ok=True)
    lien.symlink_to(cible)

    with _session(sessions) as db:
        resultat = integrite.purger_orphelins_blobs_expires(db)

    assert resultat["purged"] == 0
    assert cible.is_file()
    assert lien.is_symlink()


def test_la_recuperation_est_idempotente(base_depot):
    _, sessions = base_depot
    data_dir = get_data_dir()
    orphelin = _orphelin(data_dir, "blobs/zz/idempotent.bin")
    _vieillir(orphelin)

    with _session(sessions) as db:
        premier = integrite.purger_orphelins_blobs_expires(db)
        second = integrite.purger_orphelins_blobs_expires(db)

    assert premier["purged"] == 1
    assert second["purged"] == 0


# ---------- 6. Confinement du chemin `relative_path` avant `unlink` ----------


def test_un_relative_path_hors_racine_n_est_jamais_supprime(base_depot, tmp_path):
    """Une ligne `blobs` corrompue ne doit pas faire sortir `unlink` de data/."""
    from datetime import datetime, timedelta, timezone

    from backend.search import trash as T

    _, sessions = base_depot
    dehors = tmp_path / "hors-racine.bin"
    dehors.write_bytes(b"hors racine")

    with _session(sessions) as db:
        workspace_id = _espace(db, "Corrompu")
        _depot(db, workspace_id, ["corrompu.pdf"], ["corrompu"])
        blob = db.query(Blob).one()
        blob.relative_path = "../../hors-racine.bin"
        lien = (
            db.query(ArtifactLink)
            .filter_by(artifact_id=db.query(Artifact).one().id)
            .one()
        )
        lien.removed_at = datetime.now(timezone.utc) - timedelta(days=8)
        db.commit()

        rapport = T.purge(db, days=7)
        db.commit()
        assert db.query(Artifact).count() == 0

    assert rapport["artifacts"] == 1
    assert rapport["blobs_failed"] == 1
    assert any("chemin refusé" in e["error"] for e in rapport["errors"])
    assert dehors.is_file(), "le fichier hors racine ne doit jamais être touché"
