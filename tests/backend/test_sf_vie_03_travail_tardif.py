"""SF-VIE-03 — Travail tardif : un worker ne résuscite pas sa destination supprimée.

D'OÙ VIENT CE FICHIER : le cycle de vie des informations (v.1.3) — §4.8 « Traitements
tardifs » : « Une ingestion, une analyse ou une génération peut terminer après une
annulation ou suppression. Avant enregistrement, le backend vérifie la destination,
le statut de la tâche et les permissions courantes. » puis « Un résultat tardif ne
recrée pas silencieusement une discussion, une ressource supprimée ou un
rattachement disparu. » ; §5.2 scénario d'acceptation : « Terminer un worker après
suppression : aucune résurrection implicite. » ; §4.7 : « Une suppression doit
couvrir toutes les voies par lesquelles la ressource resterait consultable : index,
représentations, miniatures, résultats mis en cache, fichiers temporaires et
workers actifs. » Registre des scénarios
(`docs/intentions/validation/scenarios_fonctionnels.md` §4, SF-VIE-03) :
« Action : supprimer la destination ou annuler, puis laisser terminer le worker.
Attendu : aucune résurrection de ressource ou publication non autorisée ; résultats
existants traités selon le cycle de vie. Observation : ordre des événements et
vérification d'enregistrement. »

CE QUE LE TEST VERROUILLE — ce que le code dit déjà, mesuré contre l'ordre des
événements du scénario (l'audit a trouvé un verrou réel à chaque écrivain de
l'index, pas un test à faire plier) :

  - `backend/search/observations.py:record_tool_observation` — l'enregistrement de
    fin d'un worker d'ingestion : sous liens `removed_at IS NULL` seulement. La
    révision de fiche passe par `cards.revise_card_after_observation`, qui
    réindexe par lien ACTIF ; les fragments (passages) ne s'ouvrent que pour le
    couple (scope_kind, scope_id) des liens actifs, et ne s'exécutent pas s'il
    n'y en a aucun ; un artefact absent de la base rend simplement `None` — une
    ressource purgée ne recrée pas sa trace.
  - `backend/services/depot_integrite.py` — le réindexeur de maintenance ne
    répare que les liens VIVANTS (`_vivants`) ; ses détecteurs écartent les
    liens morts et les artefacts supprimés (« remettre en index un artefact
    supprimé nuirait sans rien réparer »).
  - `backend/search/fts.py:reparer_ingestions_binaire_ou_tronquees` — ne réécrit
    jamais : il purge (`remove_artifact`, `remove_chunks_for_artifact`), marque
    `stale`, et sa relance éventuelle repasse par le chemin gardé ci-dessus.
  - `backend/search/docrefs.py:allocate_doc_label` — « un artefact supprimé ou
    retiré (lien `removed_at`) lève au lieu de récupérer un `doc_N` » : pas de
    ré-publication par le petit bout de l'étiquette.

Quatre blocs, un par déclencheur : la corbeille de la discussion porteuse, la
purge du GC (`trash.purge`), le retrait du dépôt (`unshare_artifact`), la
maintenance d'index en aval du retrait. Le « worker » est rejoué à l'appel près
qui termine son travail — `record_tool_observation`, l'écriture exacte que
`ingestion/attachment_ingest._ingérer_un` fait après une extraction — dans
l'ordre des événements du scénario : destination supprimée d'abord, worker derrière.

Montage : le même que `test_etat_ingestion.py` — base neuve et jetable par test,
`SessionLocal` du projet redirigée, déclenchement d'ingestion neutralisé (aucun
fil réel). Aucun modèle, aucun GPU, aucun réseau.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text as sql_text
from sqlalchemy.orm import sessionmaker

from backend.db import database as database_module
from backend.db.models import Artifact, ArtifactLink, Base, Blob, Conversation
from backend.ingestion import etat
from backend.ingestion import attachment_ingest
from backend.ipc import attachments as A
from backend.search import fts as _fts
from backend.search import trash as registre
from backend.services import corbeille as corbeille_service
from backend.services import depot_integrite as integrite
from backend.services import espaces as service

# Un texte plus long que la borne d'ouverture des fragments (200 caractères) :
# sans lui, le test prouverait une absence qui n'aurait jamais pu naître.
TEXTE_TARDIF = (
    "Passages extraits par un worker parti avant la suppression de sa "
    "destination, jamais enregistrés ni relus tant que le lien est vivant. "
) * 4
assert len(TEXTE_TARDIF) > 200


@pytest.fixture()
def base(tmp_path, monkeypatch):
    """Base neuve, `SessionLocal` du projet redirigée, racines de données jetables."""
    engine = create_engine(f"sqlite:///{tmp_path / 'vie03.db'}")
    Base.metadata.create_all(bind=engine)
    from backend.db.database import _ensure_fts
    _ensure_fts(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, autocommit=False,
                            expire_on_commit=False)
    monkeypatch.setattr(database_module, "SessionLocal", sessions)
    monkeypatch.setenv("THOT_PORTABLE", "1")
    monkeypatch.setenv("THOT_APP_DATA_DIR", str(tmp_path / "app"))
    monkeypatch.setattr(attachment_ingest, "lancer_ingestion", lambda ids, **kw: None)
    return engine, sessions


def _espace(db, nom: str = "Atelier") -> str:
    return str(service.create_workspace(db, nom).id)


def _verser(db, dest: dict, nom: str, octets: bytes, request_id: str = "vie03") -> str:
    """Le VRAI chemin de dépôt : staging puis promotion. Un artefact fabriqué à la
    main ne prouverait rien du contrat de dépôt."""
    item = A.stage_picker_bytes(db, {"request_id": request_id, "filename": nom,
                                     "bytes": octets})["items"][0]
    (artifact_id,) = A.attachments_prepare(db, {
        **dest, "stageItemIds": [item["stage_item_id"]], "names": [nom],
    })["artifact_ids"]
    return artifact_id


def _fiche_et_index(db, artifact_id: str) -> None:
    """Fiche rev 1 + ligne d'index par lien actif : l'inventaire de départ, un état
    vivant dont le test mesure la disparition."""
    from backend.search import cards

    artefact = db.query(Artifact).filter(Artifact.id == artifact_id).one()
    cards.ensure_card_on_import(db, artefact)
    db.commit()


def _passages(db, artifact_id: str) -> tuple[int, int]:
    """(lignes d'index, fragments) enregistrés pour l'artefact, toute portée."""
    index = db.execute(sql_text(
        "SELECT COUNT(*) FROM artifact_search WHERE artifact_id = :a"),
        {"a": artifact_id}).scalar_one()
    fragments = db.execute(sql_text(
        "SELECT COUNT(*) FROM chunk_search WHERE artifact_id = :a"),
        {"a": artifact_id}).scalar_one()
    return index, fragments


def _travail_tardif(artifact_id: str, *, ok: bool = True):
    """Le worker RÉINVESTI à la fin : l'appel exact que `_ingérer_un` fait après
    l'extraction — l'observation est un registre, ses ENRICHISSEMENTS des
    passages. Aucun fil, aucun placard : ce qui compte est l'horloge réelle, la
    destination fait partie de l'index S'IL y a un lien actif."""
    from backend.search import observations as _obs
    return _obs.record_tool_observation(
        artifact_id, "attachment_ingest", None, TEXTE_TARDIF, ok=ok)


# ---------- 1. Corbeille de la discussion porteuse : le worker finit après ----------

def test_corbeille_puis_travail_tardif_les_passages_ne_reviennent_pas(base):
    """SF-VIE-03 par le chemin de production de la corbeille : la discussion est
    mise en corbeille — ce qui retire ses liens ET ses entrées d'index de portée
    conversation (§4.1 « retirés ou invalidés ») — puis le travail d'ingestion
    tardif termine : aucune ligne d'index ne réapparaît, aucun fragment ne se
    ré-enregistre."""
    _, sessions = base
    with sessions() as db:
        espace = _espace(db)
        conv = Conversation(id="conv_morte1", title="Salle du temps", mode="chat")
        db.add(conv)
        db.commit()
        artifact_id = _verser(db, {"conversationId": conv.id}, "en_attente.csv",
                              TEXTE_TARDIF.encode("utf-8"))
        _fiche_et_index(db, artifact_id)
    with sessions() as db:
        avant = _passages(db, artifact_id)
        assert avant[0] > 0, "l'index de départ : le test ne vérifie pas un vide"

        rapport = corbeille_service.mettre_en_corbeille(db, "conv_morte1")
        assert rapport["ok"] and not rapport["already_trashed"]
        apres_corbeille = _passages(db, artifact_id)
        assert apres_corbeille[0] < avant[0], (
            "la corbeille retire les entrées d'index de la portée supprimée")

        observation = _travail_tardif(artifact_id)
        db.commit()
        assert observation is not None, "la trace tardive s'écrit (traçabilité §5.1)"
    with sessions() as db:
        assert _passages(db, artifact_id) == apres_corbeille, (
            "le travail tardif n'enregistre aucun NOUVEAU passage recherché")


def test_corbeille_puis_echec_tardif_consigne_sans_passage(base):
    """Un worker dont l'extraction a ÉCHOUÉ et qui ne consigne que son motif : le
    même verrou tient — un échec tardif ne publie pas davantage."""
    _, sessions = base
    with sessions() as db:
        conv = Conversation(id="conv_morte2", title="Salle 2", mode="chat")
        db.add(conv)
        db.commit()
        artifact_id = _verser(db, {"conversationId": conv.id}, "echoue.csv",
                              TEXTE_TARDIF.encode("utf-8"))
        _fiche_et_index(db, artifact_id)
    with sessions() as db:
        corbeille_service.mettre_en_corbeille(db, "conv_morte2")
    with sessions() as db:
        _travail_tardif(artifact_id, ok=False)
        db.commit()
    with sessions() as db:
        assert _passages(db, artifact_id) == (0, 0)


def test_corbeille_puis_exigence_de_label_public_refusee(base):
    """Le verrou en amont : un travail tardif qui réexigerait un `doc_N` lève.
    `docrefs.allocate_doc_label` : « un artefact supprimé ou retiré (lien
    `removed_at`) lève au lieu de récupérer un `doc_N` » — pas de re-publication
    par le petit bout de l'étiquette."""
    _, sessions = base
    with sessions() as db:
        conv = Conversation(id="conv_morte3", title="Salle 3", mode="chat")
        db.add(conv)
        db.commit()
        artifact_id = _verser(db, {"conversationId": conv.id}, "morte.csv",
                              TEXTE_TARDIF[:300].encode("utf-8"))
    with sessions() as db:
        corbeille_service.mettre_en_corbeille(db, "conv_morte3")
        from backend.search import docrefs
        try:
            docrefs.allocate_doc_label(db, "conv_morte3", artifact_id)
        except ValueError as levée:
            message = str(levée)
        else:
            message = ""
    assert "retiré" in message, "l'allocation refuse le lien retiré, nommément"


# ---------- 2. Purge du GC : la ressource purgée ne se recrée pas ----------

def test_purge_gc_puis_travail_tardif_rend_none_et_n_ecrit_rien(base):
    """`trash.purge` détruit l'artefact sans lien actif (lignes FTS, cartes,
    artefact). Le worker qui finit APRÈS n'a plus rien à enregistrer :
    `record_tool_observation` rend `None` — une ressource purgée n'a pas de trace,
    et le test vérifie qu'elle n'en a pas plus."""
    _, sessions = base
    with sessions() as db:
        espace = _espace(db)
        artifact_id = _verser(db, {"workspaceId": espace}, "purge.csv",
                              TEXTE_TARDIF.encode("utf-8"))
        _fiche_et_index(db, artifact_id)
        lien = db.query(ArtifactLink).filter(
            ArtifactLink.artifact_id == artifact_id).one()
        lien.removed_at = etat.utc_naif()  # la corbeille suffit : `days=0` l'échue

        rapport = registre.purge(db, days=0)
        assert rapport["artifacts"] == 1, "le GC emporte l'artefact sans lien actif"
        db.commit()
        assert db.query(Artifact).filter(Artifact.id == artifact_id).first() is None

        rendu = _travail_tardif(artifact_id)
        db.commit()
        assert rendu is None, "une ressource purgée ne reçoit pas de trace"
    with sessions() as db:
        assert db.query(Artifact).filter(Artifact.id == artifact_id).first() is None
        assert _passages(db, artifact_id) == (0, 0)


# ---------- 3. Retrait du dépôt : le travail finit après, rien ne revient ----------

def test_retrait_depot_puis_travail_tardif_aucune_nouvelle_ligne(base):
    """`unshare_artifact` (le chemin réel `espace.depot_retirer`) — la source est
    retirée et devient `obsolète`. Un travail d'ingestion commencé AVANT le
    retrait termine après : son enregistrement n'ajoute AUCUNE ligne au registre
    de recherche, et l'état de la source reste `obsolète`."""
    _, sessions = base
    with sessions() as db:
        espace = _espace(db)
        artifact_id = _verser(db, {"workspaceId": espace}, "retire.csv",
                              TEXTE_TARDIF.encode("utf-8"))
        _fiche_et_index(db, artifact_id)
    with sessions() as db:
        service.unshare_artifact(db, espace, artifact_id)
        avant, _ = _passages(db, artifact_id)

        _travail_tardif(artifact_id)
        db.commit()
    with sessions() as db:
        index, fragments = _passages(db, artifact_id)
        assert index == avant, (
            "le travail tardif n'enregistre aucun NOUVEAU passage pour une "
            "destination supprimée du dépôt")
        assert fragments == 0
        etat_lu = etat.lire(db.query(Artifact).filter(Artifact.id == artifact_id).one())
        assert etat_lu.etat == etat.ETAT_OBSOLETE, (
            "l'état de la source retirée reste `obsolète` : le travail tardif ne "
            "le reprend pas")


def test_retrait_depot_puis_maintenance_ne_repare_que_les_vivants(base):
    """La maintenance en aval du retrait : `reparer_depot` ne réindexe que les
    liens VIVANTS (détecteur `index_manquant`, les liens morts exclus), et la
    réparation d'une ingestion binaire purge sans jamais réécrire — repasser les
    deux maintenances sur la source retirée n'écrit pas un passage de plus."""
    _, sessions = base
    with sessions() as db:
        espace = _espace(db)
        artifact_id = _verser(db, {"workspaceId": espace}, "entretien.csv",
                              TEXTE_TARDIF.encode("utf-8"))
        _fiche_et_index(db, artifact_id)
        service.unshare_artifact(db, espace, artifact_id)
        avant, _ = _passages(db, artifact_id)

        rapport = integrite.verifier_depot(db, espace)
        manquants = rapport["anomalies"].get("index_manquant", [])
        assert not [a for a in manquants if a.get("artifact_id") == artifact_id], (
            "les liens morts ne sont pas des index_manquant à réparer")

        integrite.reparer_depot(db)
        assert _passages(db, artifact_id)[0] == avant, (
            "la maintenance réindexe des liens vivants ; rien ne réécrit pour "
            "le retiré")

        # Une OBSERVATION vraiment binaire, pour que la détection agisse : la
        # signature ZIP survit au décodage « latin-1 » (réversible sur 0x00-0xFF),
        # tout comme un contenu binaire ingéré avant la réparation.
        extraction_binaire = ("PK\x03\x04 le contenu binaire d'un ZIP relu "
                              "comme texte." + "\x00" * 300)
        from backend.search import observations as _obs
        _obs.record_tool_observation(artifact_id, "attachment_ingest", None,
                                     extraction_binaire, ok=True)
        db.commit()
        _fts.reparer_ingestions_binaire_ou_tronquees(db, appliquer=True)
        db.commit()
    with sessions() as db:
        index, fragments = _passages(db, artifact_id)
        assert index == 0 and fragments == 0, (
            "la réparation d'ingestion purge la ligne restante, jamais elle ne "
            "la réécrit : le compte descend, il ne remonte pas")
