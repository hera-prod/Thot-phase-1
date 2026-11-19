"""backend/ingestion/reprise.py — le balayage de reprise, au démarrage.

D'OÙ VIENT CE FICHIER : d'un état qui ne se refermait pas. Une extraction d'OCR
interrompue par l'arrêt de l'application laissait l'artefact en `en_cours` — ou, pire,
sans aucune marque du tout — jusqu'à ce qu'une action manuelle le remarque. Rien ne le
rattrapait : ni au démarrage suivant, ni jamais. Le fichier restait dans son état
indéterminé, ce qui est précisément ce que la Bible refuse (2.6).

OÙ VA QUOI : `demarrer_reprise()` est appelée par `main.py` au démarrage, juste après
`_replay_pending_artifact_events` et pour la même raison — rattraper ce qu'un arrêt a
laissé en suspens. Elle ne lève jamais : un balayage qui casse le démarrage pour un
fichier illisible transformerait un problème consultable en application indisponible.

LES DEUX PARAMÈTRES TRANCHÉS, ET POURQUOI.

1. `SEUIL_EN_COURS_S = 900` — un `en_cours` plus vieux que quinze minutes est mort.

   Au moment du balayage, aucun fil d'extraction ne tourne dans CE processus : il vient
   de démarrer. Un `en_cours` rencontré à froid appartient donc à une exécution
   précédente, ou à une AUTRE instance qui partage la base (mode portable, deux
   fenêtres). Le seuil n'existe que pour ce second cas — il borne le prix d'une erreur,
   et ce prix se chiffre : relancer une extraction coûte du temps machine et échoue
   peut-être sur une machine incapable, alors que ne rien lancer laisse le fichier dans
   un état DÉCIDÉ et visible. Nous acceptons donc de risquer une double extraction
   après quinze minutes d'inactivité pour ne jamais laisser un fichier coincé.

   Pourquoi 900 et pas 60 : une OCR de vingt pages dépasse la minute sans effort, et un
   seuil trop court ferait rejouer par le balayage le travail légitime d'une autre
   instance. Pourquoi pas 3600 : au-delà, un rattrapage ne servirait qu'au lendemain,
   c'est-à-dire jamais. La borne est un compromis explicite, pas une constante oubliée.

2. `modeles_autorises = False` — le balayage ne charge AUCUN modèle.

   Un démarrage qui attend un chargement d'OCR est un démarrage qui semble gelé, et qui
   échoue entièrement sur une machine sans modèle. `lancer_ingestion` reçoit donc
   `modeles_autorises=False` : l'extraction avance jusqu'au point exact où le modèle
   serait nécessaire, puis s'arrête et rend la dette — état `depose` et motif
   journalisés. Les fichiers qui n'ont besoin d'aucun modèle (tableurs, fichiers texte,
   PDF à couche texte) sont repris normalement, sans coût et sans attente.

   La dette différée est journalisée par `_ingérer_un` LUI-MÊME, qui est le seul à
   savoir qu'il vient de refuser un modèle ; ce module ne peut donc pas la compter, et
   ne prétend pas le faire. Ce qu'il sait — les repris, les ignorés, le nombre
   d'éligibles — il le rend.

BORNES. `MAX_ARTEFACTS_PAR_PASSE = 50` artefacts par démarrage, triés par ordre
d'arrivée, donc déterministe. Une base de dix mille fichiers en attente ne lance pas
mille fils d'extraction d'un coup au démarrage : elle en solde cinquante par
démarrage, et le reste arrive aux suivants. C'est le compromis entre « tout rattraper »
et « démarrer vite », et il est explicite.

UNE PASSE, PAS UNE BOUCLE. Le balayage ne réexamine jamais un artefact qu'il vient de
lancer : `_en_cours` le protège le temps du fil, et une passe qui bouclerait sur elle-même
relancerait indéfiniment la même extraction.

REPRISE PÉRIODIQUE (tâche 2.4). `demarrer_reprise_periodique` ajoute un timer daemon
dans le sidecar : un tick par minute, `modeles_autorises=True`, cinq artefacts au plus.
Il ne prend AUCUN bail GPU lui-même — les fils d'ingestion le font — et s'abstient si
`tour_actif()` ou si le manager n'est pas prêt. Le point de démarrage exact est
`backend/main.py`, juste après `demarrer_reprise` et avant `stdin_loop`.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

# Un `en_cours` plus vieux que ceci est considéré comme abandonné. Voir le module.
SEUIL_EN_COURS_S = 900

# Artefacts repris AU MAXIMUM par passe de balayage. Voir le module.
MAX_ARTEFACTS_PAR_PASSE = 50

# Reprise périodique (tâche 2.4) : 5 artefacts par minute, jamais plus. Un tick
# par minute avec cette borne dit exactement le débit ; une boucle plus rapide
# ne ferait que relire la base pour ne rien reprendre.
INTERVALLE_REPRISE_PERIODIQUE_S = 60
LIMITE_REPRISE_PERIODIQUE = 5

# Les états qu'une passe va chercher. `inconnu` y est : c'est le « on ne sait pas » des
# artefacts antérieurs au relevé, et on ne le solde QUE pour ceux qui n'ont aucune
# observation d'ingestion réussie — sinon le premier démarrage suivant la migration
# rejouerait tout un patrimoine déjà extrait, pour rien.
ETATS_CIBLES: tuple[str, ...] = ("depose", "en_cours", "inconnu", "partiel")

# Construit UNE fois : la liste est interpolée, ce qui est sûr parce qu'elle ne contient
# que des littéraux de ce module — et le dire évite de le recomposer à chaque passe.
_CONDITION_ETATS = ",".join(f"'{etat}'" for etat in ETATS_CIBLES)


def _candidats(session, *, limite: int, maintenant=None) -> list[dict]:
    """UNE lecture : les artefacts à rejouer, triés par ordre d'arrivée.

    Le tri n'est pas décoratif — c'est ce qui rend la passe DÉTERMINISTE. Sans lui,
    deux démarrages successifs pourraient solder les mêmes cinquante fichiers et
    laisser les suivants pour plus tard, indéfiniment.

    La sous-requête sur `observations` distingue « jamais regardé » de « regardé avant
    le relevé » : ce n'est pas une jointure de confort, c'est la condition qui empêche
    la migration de déclencher une vague de réingestion.

    `ingest_next_attempt_at` est le backoff : une dette dont l'échéance est future
    n'est pas candidate, même si son état est rejouable. L'échéance est comparée en
    UTC naïf, le seul format que produisent `func.now()` et `etat.utc_naif()`.
    """
    from sqlalchemy import text as _text
    from backend.ingestion import etat as _etat

    return [
        dict(ligne)
        for ligne in session.execute(
            _text(f"""
        SELECT a.id AS id, a.kind AS kind,
               a.ingest_state AS etat,
               a.ingest_updated_at AS maj,
               a.ingest_heartbeat_at AS battement,
               a.ingest_next_attempt_at AS prochaine
        FROM artifacts a
        WHERE a.deleted_at IS NULL
          AND a.ingest_state IN ({_CONDITION_ETATS})
          AND (a.ingest_next_attempt_at IS NULL
               OR a.ingest_next_attempt_at <= :maintenant)
          AND (a.ingest_state <> 'inconnu'
               OR NOT EXISTS (SELECT 1 FROM observations o
                              WHERE o.artifact_id = a.id AND o.status = 'completed'
                                AND o.tool_name = 'attachment_ingest'))
        ORDER BY a.created_at, a.id
        LIMIT :limite
    """),
            {
                "limite": max(1, int(limite)),
                "maintenant": _etat.utc_naif(maintenant),
            },
        )
        .mappings()
        .all()
    ]


def balayer_reprise(
    *,
    seuil_s: int = SEUIL_EN_COURS_S,
    limite: int = MAX_ARTEFACTS_PAR_PASSE,
    modeles_autorises: bool = False,
) -> dict:
    """Rejoue les états non terminés. Ne lève JAMAIS. Rend le bilan de la passe.

    Renvoie `{ok, eligibles, repris, ignores, seuil_s, limite, modeles_autorises}`.
    Le bilan est RENDU et pas seulement journalisé : un appelant doit pouvoir décider
    s'il propose quelque chose à l'utilisateur sans relire le journal. Il ne contient
    PAS la liste des différés faute de modèle — `_ingérer_un` est seul à savoir qu'il
    vient d'en refuser un, et ce module ne l'invente pas.
    """
    from backend.db.database import SessionLocal
    from backend.ingestion import attachment_ingest
    from backend.ingestion import etat as _etat

    bilan: dict[str, Any] = {
        "ok": True,
        "eligibles": 0,
        "repris": [],
        "ignores": [],
        "seuil_s": seuil_s,
        "limite": limite,
        "modeles_autorises": modeles_autorises,
    }
    session = SessionLocal()
    try:
        try:
            candidats = _candidats(session, limite=limite)
        except Exception as exc:
            # La base est illisible : on le DIT et on rend un bilan vide. `eligibles: 0`
            # serait alors un mensonge, donc la ligne de journal est ce qui compte —
            # c'est elle qui distingue « rien à reprendre » de « je n'ai pas pu
            # regarder », la seule distinction qui vaille ici.
            log.warning(
                "[reprise] balayage impossible : %s: %s", type(exc).__name__, exc
            )
            return bilan
        bilan["eligibles"] = len(candidats)
        a_rejouer = _a_rejouer(candidats, seuil_s=seuil_s, ignores=bilan["ignores"])
        if not a_rejouer:
            return bilan
        # Un `en_cours` périmé peut encore être retenu par le garde en mémoire
        # du processus : on le libère AVANT de relancer, sinon `lancer_ingestion`
        # le filtrerait et la dette resterait invisible.
        attachment_ingest.liberer_en_cours_expires(a_rejouer, seuil_s=seuil_s)
        try:
            attachment_ingest.lancer_ingestion(
                a_rejouer, modeles_autorises=modeles_autorises
            )
        except Exception as exc:
            log.warning(
                "[reprise] relance refusée pour %d artefact(s) : %s: %s",
                len(a_rejouer),
                type(exc).__name__,
                exc,
            )
            bilan["ignores"].extend(
                {
                    "artifact_id": a,
                    "raison": f"relance refusée ({type(exc).__name__}: {exc})",
                }
                for a in a_rejouer
            )
            return bilan
        bilan["repris"] = a_rejouer
        return bilan
    finally:
        try:
            session.close()
        except Exception:
            pass


def _a_rejouer(
    candidats: list[dict], *, seuil_s: int, ignores: list[dict]
) -> list[str]:
    """Sépare ce qu'on rejoue de ce qu'on laisse, et CHAQUE motif va dans `ignores`.

    Pur : la liste est une sortie, donc un appelant peut la lire sans fouiller le
    journal. Le seuil ne s'applique qu'à `en_cours` : un `depose` n'a jamais commencé,
    donc son âge ne dit rien, et un `inconnu` n'a pas de date du tout.
    """
    from backend.ingestion import etat as _etat

    a_rejouer: list[str] = []
    for candidat in candidats:
        artifact_id = str(candidat.get("id") or "")
        if not artifact_id:
            continue
        if str(candidat.get("etat") or "") != _etat.ETAT_EN_COURS:
            a_rejouer.append(artifact_id)
            continue
        age = _etat.age_secondes(candidat.get("battement") or candidat.get("maj"))
        if age is not None and age < seuil_s:
            # Une autre instance travaille peut-être encore dessus. Le dire est
            # indispensable : sans cette ligne, son absence ressemble à un oubli.
            ignores.append(
                {
                    "artifact_id": artifact_id,
                    "raison": f"extraction encore récente ({int(age)} s) : "
                    "une autre instance s'en occupe",
                }
            )
            continue
        a_rejouer.append(artifact_id)
    return a_rejouer


def demarrer_reprise(**kwargs) -> dict:
    """Le point d'entrée du démarrage : journalise, ne lève pas.

    LE JOURNAL EST LA FONCTION, pas un supplément. Un rattrapage invisible est
    exactement le défaut que ce fichier existe pour supprimer : il faut donc pouvoir
    lire « 3 fichiers repris » dans le journal d'un démarrage, sans ouvrir la base.
    Le silence n'est réservé qu'au cas où il n'y a RIEN à reprendre — un `debug`
    suffit alors, et un `info` à chaque démarrage noierait le journal des
    applications portables dans une ligne qui ne dit rien.
    """
    try:
        bilan = balayer_reprise(**kwargs)
    except Exception as exc:  # noqa: BLE001 — un rattrapage ne doit rien casser
        log.warning("[reprise] balayage interrompu : %s: %s", type(exc).__name__, exc)
        return {
            "ok": False,
            "eligibles": 0,
            "repris": [],
            "ignores": [],
            "erreur": f"{type(exc).__name__}: {exc}",
        }

    if not bilan["repris"] and not bilan["ignores"]:
        log.debug("[reprise] aucune ingestion à reprendre")
        return bilan
    log.info(
        "[reprise] %d artefact(s) repris (modèles %s), %d ignoré(s) — "
        "borne de la passe : %d",
        len(bilan["repris"]),
        "autorisés" if bilan["modeles_autorises"] else "refusés",
        len(bilan["ignores"]),
        bilan["limite"],
    )
    for artifact_id in bilan["repris"]:
        log.info("[reprise] %s : extraction relancée", artifact_id)
    for motif in bilan["ignores"]:
        log.info("[reprise] %s : ignoré — %s", motif["artifact_id"], motif["raison"])
    return bilan


# Reprise périodique : un seul thread par processus, arrêtable par variable
# d'environnement. Le verrou protège le démarrage/arrêt, pas le travail.
_thread_periodique: threading.Thread | None = None
_stop_periodique = threading.Event()
_lock_periodique = threading.Lock()


def _boucle_periodique(
    stop_event: threading.Event, est_pret: Callable[[], bool] | None
) -> None:
    """Un tick par minute : reprise avec modèles, si la machine est libre.

    AUCUNE attente de bail GPU ici : le timer ne fait que lancer la passe, les
    fils d'ingestion prennent (ou refusent) leur bail. Un `tour_actif()` vrai
    n'est pas une erreur — on réessaie au tick suivant, sans exception.
    """
    from backend.services.activity import tour_actif

    while not stop_event.wait(INTERVALLE_REPRISE_PERIODIQUE_S):
        try:
            if stop_event.is_set():
                return
            if tour_actif():
                continue
            if est_pret is not None and not est_pret():
                continue
            bilan = balayer_reprise(
                limite=LIMITE_REPRISE_PERIODIQUE,
                modeles_autorises=True,
            )
            if bilan.get("repris"):
                log.info(
                    "[reprise] périodique : %d artefact(s) repris (modèles autorisés)",
                    len(bilan["repris"]),
                )
        except Exception as exc:  # noqa: BLE001 — un fond ne casse jamais le sidecar
            log.warning("[reprise] périodique : %s: %s", type(exc).__name__, exc)


def demarrer_reprise_periodique(
    *, est_pret: Callable[[], bool] | None = None
) -> dict[str, Any]:
    """Démarre le timer daemon, une seule fois. Désactivable par variable d'env.

    Le point de démarrage est `backend/main.py`, juste après le balayage de
    démarrage (`demarrer_reprise`) et avant la boucle stdin. Le timer attend un
    manager prêt (`est_pret`) : charger un modèle à froid au démarrage n'est pas
    son rôle.
    """
    global _thread_periodique, _stop_periodique
    if os.environ.get("THOT_DISABLE_INGEST_REPRISE") == "1":
        return {"ok": True, "statut": "desactive"}
    with _lock_periodique:
        if _thread_periodique is not None and _thread_periodique.is_alive():
            return {"ok": True, "statut": "deja_demarre"}
        _stop_periodique = threading.Event()
        _thread_periodique = threading.Thread(
            target=_boucle_periodique,
            args=(_stop_periodique, est_pret),
            name="thot-ingest-reprise",
            daemon=True,
        )
        _thread_periodique.start()
    return {"ok": True, "statut": "demarre"}


def arreter_reprise_periodique() -> dict[str, Any]:
    """Demande l'arrêt et joint le thread au plus cinq secondes."""
    global _thread_periodique

    _stop_periodique.set()
    with _lock_periodique:
        thread = _thread_periodique
    if thread is not None:
        thread.join(5)
    with _lock_periodique:
        if (
            thread is not None
            and _thread_periodique is thread
            and not thread.is_alive()
        ):
            _thread_periodique = None
    return {"ok": True, "statut": "arrete"}


__all__ = [
    "ETATS_CIBLES",
    "INTERVALLE_REPRISE_PERIODIQUE_S",
    "LIMITE_REPRISE_PERIODIQUE",
    "MAX_ARTEFACTS_PAR_PASSE",
    "SEUIL_EN_COURS_S",
    "arreter_reprise_periodique",
    "balayer_reprise",
    "demarrer_reprise",
    "demarrer_reprise_periodique",
]
