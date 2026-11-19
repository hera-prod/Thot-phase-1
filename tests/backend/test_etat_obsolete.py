"""L'état `obsolète` : une source remplacée ou retirée se lit dans le relevé.

D'OÙ VIENT CE FICHIER : la Décision D5 consignée dans
`docs/intentions/mecanismes/cycle_de_vie_des_informations.md` §3.1 (version
d'octobre 2026) — « L'état `obsolète` est ajouté au vocabulaire d'ingestion.
Définition : la source a été remplacée ou retirée. Le document reste consultable
dans son dernier état connu, cesse d'être indexé, et n'est plus proposé à la
recherche ni aux outils. Sa citation dans une réponse antérieure reste valable :
la traçabilité ne s'efface pas. Une réingestion crée un état neuf, sans altérer
le précédent. »

Trois questions, trois blocs :

  1. LE VOCABULAIRE — `obsolète` appartient à `ETATS`, se pose via `marquer`, se
     lit via `lire`, est terminal (le balayage de reprise n'a rien à rejouer), et
     la porte du vocabulaire fermé reste ferme : ce qui n'existe pas lève toujours.
  2. L'ÉCRITURE AU RETRAIT — retirer un fichier du dépôt (le chemin réel
     `espace.depot_retirer` → `espaces.unshare_artifact`) écrit `obsolète` quand
     le DERNIER lien actif disparaît ; ne l'écrit PAS quand l'artefact vit
     encore dans une autre portée ; et reste idempotent face à un retrait déjà
     consigné.
  3. LA LECTURE et LA RÉINGESTION — `depot()` porte l'état par le chemin unique
     `etat.lire`, sans verbe nouveau ; un même contenu re-versé crée une
     ressource neuve, dont l'état n'altère jamais le précédent (D5).

Montage : le même que `test_etat_ingestion.py` — base neuve et jetable par test,
`SessionLocal` du projet redirigée, ingestion neutralisée (aucun fil d'extraction
ne court pendant les mesures). Aucun modèle, aucun GPU, aucun réseau.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.db import database as database_module
from backend.db.models import Artifact, ArtifactLink, Base, Conversation
from backend.ingestion import etat
from backend.ingestion import attachment_ingest
from backend.ipc import attachments as A
from backend.services import espaces as service


@pytest.fixture()
def base(tmp_path, monkeypatch):
    """Base neuve, `SessionLocal` du projet redirigée, racines de données jetables.

    L'ingestion est NEUTRALISÉE, comme dans les suites de dépôt : sans cela, la
    promotion déclencherait un vrai fil d'extraction qui écrirait son état
    (`en_cours` puis `extrait`) pendant que le test mesure — des courses dont le
    verdict dépendrait de la machine, pas du code.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'obsolete.db'}")
    Base.metadata.create_all(bind=engine)
    from backend.db.database import _ensure_fts

    _ensure_fts(engine)
    sessions = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    monkeypatch.setattr(database_module, "SessionLocal", sessions)
    monkeypatch.setenv("THOT_PORTABLE", "1")
    monkeypatch.setenv("THOT_APP_DATA_DIR", str(tmp_path / "app"))
    monkeypatch.setattr(attachment_ingest, "lancer_ingestion", lambda ids, **kw: None)
    return engine, sessions


def _espace(db, nom: str = "Atelier") -> str:
    return str(service.create_workspace(db, nom).id)


def _verser(db, dest: dict, nom: str, octets: bytes, request_id: str = "obso") -> str:
    """Le VRAI chemin de dépôt : staging puis promotion. Un artefact fabriqué à la
    main ne prouverait rien du contrat de dépôt."""
    item = A.stage_picker_bytes(
        db, {"request_id": request_id, "filename": nom, "bytes": octets}
    )["items"][0]
    (artifact_id,) = A.attachments_prepare(
        db,
        {
            **dest,
            "stageItemIds": [item["stage_item_id"]],
            "names": [nom],
        },
    )["artifact_ids"]
    return artifact_id


def _discussion_avec_ce_fichier(db, conv_id: str, artifact_id: str) -> None:
    """Un lien `conversation` actif sur l'artefact : la deuxième vie du fichier.

    Le lien est écrit directement, comme dans `test_corbeille_conversations.py` :
    ce qui compte ici, c'est l'état des LIENS, pas l'histoire des messages.
    """
    db.add(Conversation(id=conv_id, title=f"Discussion {conv_id}", mode="chat"))
    db.add(
        ArtifactLink(
            id=f"lnk_{artifact_id}_{conv_id}",
            artifact_id=artifact_id,
            scope_kind="conversation",
            scope_id=conv_id,
            role="attachment",
        )
    )
    db.commit()


def _etat_de(sessions, artifact_id: str) -> etat.Etat:
    db = sessions()
    try:
        return etat.lire(db.query(Artifact).filter(Artifact.id == artifact_id).one())
    finally:
        db.close()


def _poser(sessions, artifact_id: str, valeur: str, raison: str = "") -> None:
    db = sessions()
    try:
        db.query(Artifact).filter(Artifact.id == artifact_id).update(
            {"ingest_state": valeur, "ingest_reason": raison or None},
            synchronize_session=False,
        )
        db.commit()
    finally:
        db.close()


# ---------- 1. LE VOCABULAIRE ----------


def test_lemot_obsolete_appartient_au_vocabulaire_et_est_terminal(base):
    """D5 : « l'état `obsolète` est ajouté au vocabulaire d'ingestion ». Il EST
    dedans, il est terminal — la source n'est plus dans le périmètre, le balayage
    de reprise n'a rien à rejouer — et il n'est pas un état sans texte : il ne
    reflète ni un échec ni une extraction, il décrit la SOURCE.
    """
    assert etat.ETAT_OBSOLETE == "obsolète", "D5 nomme `obsolète`, avec son accent"
    assert etat.ETAT_OBSOLETE in etat.ETATS, "un état écrit doit être un état reconnu"
    assert etat.ETAT_OBSOLETE in etat.ETATS_TERMINAUX
    assert etat.ETAT_OBSOLETE not in etat.ETATS_NON_TERMINAUX
    # Phase 1 : une source retirée cesse d'être servie par les lecteurs, donc
    # `obsolète` appartient à `ETATS_SANS_TEXTE` — l'état qualifie ce qu'on sert,
    # pas seulement ce qui est arrivé au fichier.
    assert etat.ETAT_OBSOLETE in etat.ETATS_SANS_TEXTE
    assert etat.ETAT_OBSOLETE not in etat.ETATS_EXTRACTION_NULLE
    from backend.ingestion import reprise

    assert etat.ETAT_OBSOLETE not in reprise.ETATS_CIBLES, (
        "D5 : une réingestion crée un état NEUF ; le reprise ne re-violte pas l'ancien"
    )


def test_marquer_obsolete_accepte_et_la_porte_fermee_louvre_pas(base):
    """`marquer` est le SEUL point d'écriture : il accepte le nouvel état et
    continue de lever sur un état qui n'existe pas — ajouter au vocabulaire n'a
    jamais ramolli la porte (la loi « parse, ne valide pas » s'applique à
    l'entrée, pas à l'inventaire)."""
    _, sessions = base
    with sessions() as db:
        espace = _espace(db)
        artifact_id = _verser(
            db, {"workspaceId": espace}, "note.csv", "contenu".encode("utf-8")
        )
        marque = etat.marquer(
            db, artifact_id, etat.ETAT_OBSOLETE, "la source a été retirée du dépôt"
        )
        assert marque == etat.ETAT_OBSOLETE
        db.commit()
    etat_lu = _etat_de(sessions, artifact_id)
    assert etat_lu.etat == etat.ETAT_OBSOLETE
    assert etat_lu.est_terminal is True and etat_lu.est_reprenable is False
    assert "proposé à la recherche ni aux outils" in etat_lu.phrase

    mot = "envrac"
    with sessions() as db:
        try:
            etat.marquer(db, artifact_id, mot, "")
        except etat.EtatInconnu as levée:
            message = str(levée)
        else:
            message = ""
    assert mot in message, "un état hors vocabulaire se nomme dans l'erreur"
    assert etat.ETAT_OBSOLETE in message, "l'erreur liste le vocabulaire À JOUR"
    etat_lu = _etat_de(sessions, artifact_id)
    assert etat_lu.etat == etat.ETAT_OBSOLETE, "un refus d'écriture n'écrit rien"


def test_depot_lit_obsolete_par_le_chemin_unique(base):
    """« Lecture honnête, sans nouveau verbe » : `depot()` lit l'état par
    `backend/ingestion/etat.py:lire`, le seul chemin de lecture — donc un fichier
    `obsolète` encore servi par une ligne de dépôt se présente avec son état : un
    dépôt qui le montrerait « utilisable » mentirait là où l'utilisateur ne peut
    pas vérifier (§6 de `depot.md`).
    """
    _, sessions = base
    with sessions() as db:
        espace = _espace(db)
        artifact_id = _verser(
            db, {"workspaceId": espace}, "note.csv", "contenu".encode("utf-8")
        )
        raison = "la source a été retirée du dépôt"
        _poser(sessions, artifact_id, etat.ETAT_OBSOLETE, raison)
    with sessions() as db:
        inventaire = service.depot(db, espace)
    (ligne,) = [f for f in inventaire["files"] if f["artifact_id"] == artifact_id]
    assert ligne["ingest_state"] == etat.ETAT_OBSOLETE
    assert ligne["ingest_reason"] == raison


# ---------- 2. L'ÉCRITURE AU RETRAIT ----------


def test_retrait_dernier_lien_actif_marque_obsolete(base):
    """Le chemin réel du retrait (`espace.depot_retirer` → `unshare_artifact`) :
    quand le dernier lien actif disparaît, l'artefact devient `obsolète` avec son
    motif. D5 : « la source a été remplacée ou retirée » ; §4.3 : « Le fichier
    retiré ne doit plus alimenter de nouvelles recherches ou lectures ».
    """
    _, sessions = base
    with sessions() as db:
        espace = _espace(db)
        artifact_id = _verser(
            db, {"workspaceId": espace}, "note.csv", "contenu".encode("utf-8")
        )
    with sessions() as db:
        rapport = service.unshare_artifact(db, espace, artifact_id)
    assert rapport["ok"] is True and rapport["already_removed"] is False
    etat_lu = _etat_de(sessions, artifact_id)
    assert etat_lu.etat == etat.ETAT_OBSOLETE, "un retrait de dépôt se lit désormais"
    assert "retirée" in etat_lu.raison


def test_retrait_artefact_encore_vivant_ailleurs_ne_marque_pas(base):
    """La CONDITION se lit sur les liens : un artefact partagé au dépôt ET joint à
    une discussion vivante garde son état quand SON LIEN DE DÉPÔT part — sinon un
    retrait de dépôt écrirait « obsolète » sur un fichier que les outils continuent
    de proposer, un état qui mentirait."""
    _, sessions = base
    with sessions() as db:
        espace = _espace(db)
        artifact_id = _verser(
            db, {"workspaceId": espace}, "note.csv", "contenu".encode("utf-8")
        )
    with sessions() as db:
        _discussion_avec_ce_fichier(db, "conv_vivante", artifact_id)
    with sessions() as db:
        service.unshare_artifact(db, espace, artifact_id)
    etat_lu = _etat_de(sessions, artifact_id)
    assert etat_lu.etat == etat.ETAT_DEPOSE, (
        "l'artefact est vivant dans la discussion : sa source n'est pas retirée"
    )


def test_retrait_deja_consigne_est_idempotent(base):
    """Un retrait rejoué (réémission de l'interface) ne réécrit rien et ne
    redégénère pas le motif ; un fichier déjà `obsolète` le reste sans nouvelle
    trace."""
    _, sessions = base
    with sessions() as db:
        espace = _espace(db)
        artifact_id = _verser(
            db, {"workspaceId": espace}, "note.csv", "contenu".encode("utf-8")
        )
    with sessions() as db:
        service.unshare_artifact(db, espace, artifact_id)
    etat_lu = _etat_de(sessions, artifact_id)
    assert etat_lu.etat == etat.ETAT_OBSOLETE
    with sessions() as db:
        rejoué = service.unshare_artifact(db, espace, artifact_id)
        assert rejoué["already_removed"] is True
    etat_rejoué = _etat_de(sessions, artifact_id)
    assert etat_rejoué.etat == etat.ETAT_OBSOLETE
    assert etat_rejoué.raison == etat_lu.raison, "le motif est stable"


# ---------- 3. LA RÉINGESTION ----------


def test_reingestion_cree_un_etat_neuf_sans_alterer_le_precedent(base):
    """D5 : « une réingestion crée un état neuf, sans altérer le précédent ». Le
    chemin réel crée déjà UNE NOUVELLE ressource pour le même contenu (chaque
    promotion est un artefact ; le blob, lui, est partagé par empreinte) :
    l'ancien artefact reste `obsolète`, le nouveau démarre `depose` — l'un ne
    réécrit jamais l'autre.
    """
    _, sessions = base
    with sessions() as db:
        espace = _espace(db)
        premier = _verser(
            db,
            {"workspaceId": espace},
            "note.csv",
            "mêmes octets".encode("utf-8"),
            "obso-1",
        )
    with sessions() as db:
        service.unshare_artifact(db, espace, premier)
    with sessions() as db:
        second = _verser(
            db,
            {"workspaceId": espace},
            "note.csv",
            "mêmes octets".encode("utf-8"),
            "obso-2",
        )
        db.commit()
    assert second != premier, "la réingestion crée une ressource neuve"
    etat_ancien = _etat_de(sessions, premier)
    etat_neuf = _etat_de(sessions, second)
    assert etat_ancien.etat == etat.ETAT_OBSOLETE, "l'état précédent n'est pas altéré"
    assert etat_neuf.etat == etat.ETAT_DEPOSE, "l'état neuf n'est pas un héritage"
