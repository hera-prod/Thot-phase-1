# services/memoire_scheduler.py — l'entretien mémoire déclenché par l'inactivité.
#
# SQLite en WAL n'a qu'un seul écrivain à la fois : les sessions sont courtes,
# une par espace candidat, et refermées immédiatement. Aucun modèle n'est chargé
# ici : tout passe par `memoire_auto.executer_entretien_memoire`, déterministe.
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.services.memoire_auto import SEUIL_INACTIVITE_S, SEUIL_TOURS_MEMOIRE

log = logging.getLogger(__name__)

_INTERVALLE_S = 30
_FIL = "thot-memoire-scheduler"
_stop_event = threading.Event()
_thread: threading.Thread | None = None
_lock = threading.Lock()

# Boucle documentaire DÉDIÉE (Phase 8, tâche 8.2b) : le backfill ne dépend ni
# de la reprise d'ingestion, ni de `_candidats()` (qui exige cinq tours). Un
# espace sans aucune conversation doit recevoir sa ligne `pending` en moins de
# cinq minutes.
_INTERVALLE_BACKFILL_DOC_S = 300
_FIL_DOC = "thot-memoire-documentaire"
_stop_event_doc = threading.Event()
_thread_doc: threading.Thread | None = None
_lock_doc = threading.Lock()

# RÉVEIL IMMÉDIAT (benchmark : 85–89 % de la latence perçue venait d'attendre
# le backfill). Une ingestion committée pose cet événement ; la boucle
# documentaire l'attend AU LIEU DE dormir 300 s. L'intervalle de 5 min reste le
# filet : sans événement, le backfill périodique s'exécute comme avant.
_reveil_doc = threading.Event()
# Debounce volontairement court : il coalesce une rafale d'uploads (le même
# Event ne mémorise qu'un bit) sans rendre l'extraction perceptiblement lente.
_DEBOUNCE_DOC_S = 2.5

# SINGLE-FLIGHT de l'exécution documentaire : deux boucles (le tick de 30 s et
# la boucle dédiée de 5 min) appellent `executer_lot_documentaire`. Le CAS du
# ledger protège UN document, pas le budget global : sans ce verrou, deux passes
# pouvaient consommer 2+2 documents et 120 s. Le verrou est NON BLOQUANT : une
# passe déjà en cours fait simplement sauter le tick, jamais attendre.
_execution_doc_lock = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _candidats(db: Any, maintenant: datetime) -> list[str]:
    """Espaces dus : cinq tours comptés et inactivité d'au moins cinq minutes."""
    from sqlalchemy import func

    from backend.db.models import Workspace as _Ws

    seuil = maintenant - timedelta(seconds=SEUIL_INACTIVITE_S)
    rows = (
        db.query(_Ws.id)
        .filter(
            _Ws.state == "active",
            _Ws.auto_memory_turn_count >= SEUIL_TOURS_MEMOIRE,
            func.coalesce(_Ws.last_activity_at, _Ws.updated_at) <= seuil,
        )
        .all()
    )
    return [str(row[0]) for row in rows]


def _entretenir_un_espace(workspace_id: str) -> None:
    """Une session courte par espace, refermée quoi qu'il arrive."""
    from backend.db.database import SessionLocal
    from backend.db.models import Workspace as _Ws
    from backend.services import memoire_auto

    db = SessionLocal()
    try:
        # Re-lecture obligatoire : l'espace a pu changer entre le listage et
        # l'ouverture de cette session (nouvel échange, corbeille, compteur remis).
        ws = db.query(_Ws).filter(_Ws.id == workspace_id).first()
        if ws is None or ws.state != "active":
            return
        if int(getattr(ws, "auto_memory_turn_count", 0) or 0) < SEUIL_TOURS_MEMOIRE:
            return
        derniere = getattr(ws, "last_activity_at", None) or getattr(
            ws, "updated_at", None
        )
        if (
            derniere is not None
            and (_now() - derniere).total_seconds() < SEUIL_INACTIVITE_S
        ):
            return
        # Le wrapper décide : s'il n'obtient pas le verrou, il émet
        # `memory_deferred` sans attendre ni lever. Le planificateur ne filtre
        # donc plus sur `tour_actif()` — il ne verrait jamais le différé.
        memoire_auto.executer_entretien_memoire(
            db, workspace_id, force=False, mode="auto"
        )
        db.commit()
    except Exception as exc:  # noqa: BLE001 — le fond ne casse jamais le sidecar
        log.warning("[memoire] entretien %s impossible: %s", workspace_id, exc)
        try:
            db.rollback()
        except Exception:
            pass
        _repousser_activite(workspace_id)
    finally:
        try:
            db.close()
        except Exception:
            pass


def _repousser_activite(workspace_id: str) -> None:
    """Évite qu'un espace en échec soit retenté à chaque réveil du planificateur."""
    from backend.db.database import SessionLocal
    from backend.db.models import Workspace as _Ws

    db = SessionLocal()
    try:
        ws = db.query(_Ws).filter(_Ws.id == workspace_id).first()
        if ws is not None:
            ws.last_activity_at = _now()
            db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        try:
            db.close()
        except Exception:
            pass


def _boucle(stop_event: threading.Event) -> None:
    from backend.db.database import SessionLocal

    while not stop_event.wait(_INTERVALLE_S):
        try:
            db = SessionLocal()
            try:
                ids = _candidats(db, _now())
            finally:
                try:
                    db.close()
                except Exception:
                    pass
            for workspace_id in ids:
                if stop_event.is_set():
                    return
                _entretenir_un_espace(workspace_id)
            if not stop_event.is_set():
                _reessayer_provenance()
                # APRÈS l'entretien des messages : les documents se sélectionnent
                # uniquement par le ledger, jamais par les cinq tours.
                _executer_documents()
        except Exception as exc:  # noqa: BLE001
            log.warning("[memoire] planificateur: %s", exc)


def _backfiller_documents() -> None:
    """Recrée les lignes de ledger manquantes — idempotent, borné, journalisé."""
    from backend.services import memoire_documentaire as _md

    try:
        compteurs = _md.backfiller_ledger_documentaire()
        if compteurs.get("crees"):
            log.info("[memoire-doc] backfill : %s", compteurs)
    except Exception as exc:  # noqa: BLE001 — le fond ne casse pas le sidecar
        log.warning("[memoire-doc] backfill impossible: %s", exc)


def _executer_documents() -> None:
    """Une passe documentaire bornée (2 documents / 60 s), single-flight."""
    from backend.services import memoire_documentaire as _md

    if not _execution_doc_lock.acquire(blocking=False):
        # Une passe est déjà en cours : le tick suivant reprendra. Le CAS du
        # ledger protège le document, ce verrou protège le BUDGET global.
        return
    try:
        _md.executer_lot_documentaire()
    except Exception as exc:  # noqa: BLE001
        log.warning("[memoire-doc] exécution impossible: %s", exc)
    finally:
        _execution_doc_lock.release()


def reveiller_extraction_documentaire() -> None:
    """Signale qu'un document vient d'être enfilé, SANS jamais bloquer ni lever.

    Appelée après le commit d'un enqueue (`memoire_documentaire`) ou d'un
    partage (`espaces`). Un `Event` ne mémorise qu'un bit : cinq uploads
    rapprochés réveillent UNE passe, pas cinq. Le verrou single-flight reste
    la garde contre deux passes concurrentes.
    """
    try:
        _reveil_doc.set()
    except Exception:  # pragma: no cover — Event.set ne lève pas, mais le fond est sûr
        pass


def _boucle_documentaire(stop_event: threading.Event) -> None:
    """Backfill dédié, réveillé immédiatement par un enqueue.

    Sans événement : backfill + passe toutes les 5 min (le filet). Avec
    événement : debounce court pour coalescer la rafale, puis UNE passe. L'arrêt
    pose aussi l'événement, sinon le join attendrait jusqu'à 300 s.
    """
    _backfiller_documents()
    _executer_documents()
    while not stop_event.is_set():
        reveille = _reveil_doc.wait(_INTERVALLE_BACKFILL_DOC_S)
        _reveil_doc.clear()
        if stop_event.is_set():
            return
        if reveille:
            if stop_event.wait(_DEBOUNCE_DOC_S):
                return
            # Les événements arrivés pendant le debounce sont couverts par CETTE
            # passe : les laisser armés produirait un second tour inutile.
            _reveil_doc.clear()
            _executer_documents()
            continue
        _backfiller_documents()
        _executer_documents()


def _reessayer_provenance() -> None:
    """Rejoue les synchronisations de provenance en échec, sans boucle infinie.

    Un échec de portée a mis l'espace en file (compteur + journal). Le tick du
    planificateur est le point de reprise naturel : la panne est souvent
    transitoire (transaction concurrente), et la file est bornée par le nombre
    de tentatives du service mémoire.
    """
    from backend.db.database import SessionLocal
    from backend.services import memoire

    if not memoire.file_reprise_sync():
        return
    db = SessionLocal()
    try:
        memoire.retenter_synchronisations(db)
        db.commit()
    except Exception as exc:  # noqa: BLE001 - le fond ne casse pas le sidecar
        try:
            db.rollback()
        except Exception:
            pass
        log.warning("[memoire] reprise de provenance impossible: %s", exc)
    finally:
        try:
            db.close()
        except Exception:
            pass


def demarrer_planificateur_memoire() -> dict[str, Any]:
    """Démarre le thread daemon, une seule fois. Désactivable par variable d'env."""
    global _thread, _stop_event
    if os.environ.get("THOT_DISABLE_MEMORY_SCHEDULER") == "1":
        return {"ok": True, "statut": "desactive"}
    with _lock:
        if _thread is not None and _thread.is_alive():
            return {"ok": True, "statut": "deja_demarre"}
        # Un Event NEUF par démarrage : si l'ancien thread a été joint en timeout,
        # il ne doit pas continuer à boucler sur un Event déjà armé, ni partager
        # son signal d'arrêt avec la nouvelle boucle.
        _stop_event = threading.Event()
        _thread = threading.Thread(
            target=_boucle, args=(_stop_event,), name=_FIL, daemon=True
        )
        _thread.start()
    return {"ok": True, "statut": "demarre"}


def demarrer_planificateur_documentaire() -> dict[str, Any]:
    """Démarre la boucle documentaire dédiée (backfill 5 min + exécution).

    Séparée de `demarrer_planificateur_memoire` : elle doit tourner même si
    l'entretien des messages est désactivé, et son intervalle de backfill est
    celui du plan, pas celui du tick de 30 s.
    """
    global _thread_doc, _stop_event_doc
    if os.environ.get("THOT_DISABLE_MEMORY_DOC_SCHEDULER") == "1":
        return {"ok": True, "statut": "desactive"}
    with _lock_doc:
        if _thread_doc is not None and _thread_doc.is_alive():
            return {"ok": True, "statut": "deja_demarre"}
        _stop_event_doc = threading.Event()
        # Un signal laissé par une boucle précédente ne doit pas provoquer une
        # passe fantôme au démarrage : le backfill initial couvre déjà l'existant.
        _reveil_doc.clear()
        _thread_doc = threading.Thread(
            target=_boucle_documentaire,
            args=(_stop_event_doc,),
            name=_FIL_DOC,
            daemon=True,
        )
        _thread_doc.start()
    return {"ok": True, "statut": "demarre"}


def arreter_planificateur_documentaire() -> dict[str, Any]:
    """Arrête la boucle documentaire et joint son thread cinq secondes."""
    global _thread_doc

    _stop_event_doc.set()
    # Réveille un `wait(300)` en cours : l'arrêt ne doit pas attendre le backfill.
    _reveil_doc.set()
    with _lock_doc:
        thread = _thread_doc
    if thread is not None:
        thread.join(5)
    with _lock_doc:
        if thread is not None and _thread_doc is thread and not thread.is_alive():
            _thread_doc = None
    _reveil_doc.clear()
    return {"ok": True, "statut": "arrete"}


def arreter_planificateur_memoire() -> dict[str, Any]:
    """Demande l'arrêt et joint le thread au plus cinq secondes."""
    global _thread

    # `stop_event.set()` réveille immédiatement le `wait`, sans toucher au
    # drapeau de tour actif (qui ne nous appartient pas).
    _stop_event.set()
    with _lock:
        thread = _thread
    if thread is not None:
        thread.join(5)
    with _lock:
        # Ne jamais oublier un thread encore vivant : un `_thread = None`
        # prématuré laisserait `demarrer` créer une SECONDE boucle après un join
        # en timeout, et l'ancienne continuerait de tourner sans référence.
        if thread is not None and _thread is thread and not thread.is_alive():
            _thread = None
    return {"ok": True, "statut": "arrete"}
