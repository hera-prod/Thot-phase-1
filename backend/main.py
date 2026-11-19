"""
Thot Phase 1 — Point d'entrée du sidecar Python (JSON Lines sur stdin/stdout)
Contrats C08/C09 + pseudocode/backend_socle.md
- Thread lecteur non bloquant (ipc.py) → worker unique
- Idempotence request_id + empreinte canonique (C11)
- Un tour par conversation (C09) garanti atomique
- Annulation coopérative + escalade (C09)
- Terminal unique après persistance (C08)
"""

import sys, os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import json
import time
import threading
import hashlib
import re
from pathlib import Path

from backend.inference.ipc import (
    emit,
    emit_legacy,
    is_valid_request_id,
    log,
    stdin_loop,
    PROTOCOL_VERSION,
)
from backend.inference.manager import ModelManager
from backend.inference.resource_coordinator import ResourceBusy
from backend.db.database import init_db

# Le manager est construit ici, avant `__main__`, parce que son `backend` est
# porté par le PREMIER `backend_ready` (backend, VRAM, n_gpu_layers) et que les
# closures d'ingestion enregistrées juste après le capturent. Rendre la sonde
# matérielle paresseuse ne supprimerait donc pas l'attente : le premier
# `backend_ready` la déclencherait. La déplacer APRÈS `stdin_loop` exigerait de
# changer le protocole de démarrage (un ready sans backend), ce qui est hors du
# périmètre de la Phase 7. Le coût reste borné : `HardwareDetector.detect()` a
# un timeout de 5 s par outil et n'appelle aucun modèle.
manager = ModelManager()
_runtime = None

# Tâche 4.4 — le réexamen vision des figures PDF appelle LFM2.5-VL-3B
# (lfm2.5-vl-3b.md §3.3 : « les figures détectées sont recadrées puis
# réexaminées par le modèle de vision »). Il n'existe AUCUN singleton manager
# exposé (compaction.py : les accès directs globaux sont morts) et le fil
# d'ingestion en arrière-plan n'en a aucun manche : c'est ici, la couche qui
# sert ce manager unique, qui enregistre sa closure d'appel
# (backend/ingestion/vision_appel.py, même motif que qualification.py).
# Sans enregistrement, une figure de PDF recadrée reste un bloc repéré avec
# sa limite nommée — jamais un crash d'ingestion, jamais un modèle caché.
from backend.ingestion.vision_appel import couche_managere, enregistrer_couche_appel

enregistrer_couche_appel(couche_managere(manager))


def get_chat_runtime():
    global _runtime
    if _runtime is None:
        from backend.inference.runtime import build_runtime

        _runtime = build_runtime(manager)
    return _runtime


# Annulation : run_id -> threading.Event
_cancel_flags: dict[str, threading.Event] = {}
_cancel_lock = threading.Lock()


def get_cancel_flag(run_id: str) -> threading.Event:
    with _cancel_lock:
        if run_id not in _cancel_flags:
            _cancel_flags[run_id] = threading.Event()
        return _cancel_flags[run_id]


def clear_cancel_flag(run_id: str):
    with _cancel_lock:
        _cancel_flags.pop(run_id, None)


def shutdown_active_runs() -> None:
    global _runtime
    if _runtime is not None:
        process = getattr(_runtime, "process", None)
        if process is not None:
            try:
                process.stop()
            except Exception:
                pass
        _runtime = None
    with _cancel_lock:
        active_flags = list(_cancel_flags.values())
    for flag in active_flags:
        flag.set()
    try:
        from sqlalchemy import or_
        from backend.db.database import SessionLocal
        from backend.db.models import Run

        db = SessionLocal()
        try:
            active_status = Run.status.in_(("queued", "running", "cancelling", ""))
            rows = (
                db.query(Run)
                .filter(
                    Run.kind == "production",
                    or_(active_status, Run.status.is_(None)),
                )
                .all()
            )
            for row in rows:
                row.status = "cancelling"
            db.commit()
        finally:
            try:
                db.close()
            except Exception:
                pass
    except Exception as exc:
        log(f"[shutdown] DB cancellation failed: {_safe_public_text(exc)}")
    try:
        from backend.services.memoire_scheduler import (
            arreter_planificateur_documentaire,
            arreter_planificateur_memoire,
        )

        arreter_planificateur_documentaire()
        arreter_planificateur_memoire()
    except Exception as exc:
        log(f"[shutdown] arrêt mémoire impossible: {_safe_public_text(exc)}")
    try:
        from backend.ingestion.figures_worker import arreter_worker_figures

        arreter_worker_figures()
    except Exception as exc:
        log(f"[shutdown] arrêt des figures impossible: {_safe_public_text(exc)}")


def _canonical_fingerprint(method: str, params: dict, protocol_version: int = 1) -> str:
    """Empreinte canonique C11 : protocol_version + method + params triés + conv_id acteur"""
    # Inclure conv_id comme périmètre acteur
    conv = params.get("conv_id") or params.get("conversation_id") or ""
    payload = json.dumps(
        {"v": protocol_version, "m": method, "c": conv, "p": params},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _image_request_fingerprint(method: str, params: dict, protocol_version: int) -> str:
    canonical = dict(params or {})
    if "prompt" in canonical and isinstance(canonical["prompt"], str):
        canonical["prompt"] = canonical["prompt"].strip()
    if "aspect_ratio" in canonical and isinstance(canonical["aspect_ratio"], str):
        canonical["aspect_ratio"] = canonical["aspect_ratio"].strip()
    if "reference_ids" not in canonical and "referenceIds" in canonical:
        canonical["reference_ids"] = canonical["referenceIds"]
    canonical.pop("referenceIds", None)
    if "reference_ids" in canonical:
        references = canonical["reference_ids"]
        if isinstance(references, list):
            canonical["reference_ids"] = [
                str(value or "").strip() for value in references
            ]
        else:
            canonical["reference_ids"] = [str(references or "").strip()]
    if "idempotency_key" not in canonical and "idempotencyKey" in canonical:
        canonical["idempotency_key"] = canonical["idempotencyKey"]
    canonical.pop("idempotencyKey", None)
    if "idempotency_key" in canonical and isinstance(canonical["idempotency_key"], str):
        canonical["idempotency_key"] = canonical["idempotency_key"].strip() or None
    if "scope" in canonical and isinstance(canonical["scope"], str):
        canonical["scope"] = canonical["scope"].strip()
    # Never hash raw token values: strip staging tokens, keep their byte identity
    # through supplied SHA-256 fields instead.
    for alias in ("workToken", "work_token"):
        canonical.pop(alias, None)
    for alias in ("maskToken", "mask_token"):
        canonical.pop(alias, None)
    for alias in ("referenceTokens", "reference_tokens"):
        canonical.pop(alias, None)
    # Normalize camelCase -> snake_case for the new identity fields.
    for camel, snake in (
        ("sourceArtifactId", "source_artifact_id"),
        ("workSha256", "work_sha256"),
        ("maskSha256", "mask_sha256"),
        ("referenceSha256", "reference_sha256"),
        ("canvasWidth", "canvas_width"),
        ("canvasHeight", "canvas_height"),
        ("sessionId", "session_id"),
        ("maskKind", "mask_kind"),
        ("sourceTransform", "source_transform"),
        ("sourceSize", "source_size"),
    ):
        if camel in canonical and snake not in canonical:
            canonical[snake] = canonical.pop(camel)
    # Ensure canvas dims are ints when present (avoid string/number hash drift).
    for field in ("canvas_width", "canvas_height"):
        if field in canonical and isinstance(canonical[field], str):
            try:
                canonical[field] = int(canonical[field])
            except ValueError:
                pass
    return _canonical_fingerprint(method, canonical, protocol_version)


def _espace_actif_connu(workspace_id: str) -> bool:
    """Espace existant et non en corbeille. La corbeille est un état, pas une portée.

    Une seule source de vérité : `services.espaces.get_workspace(include_trashed=False)`
    filtre déjà l'état, donc aucune requête n'est réinventée ici. Un espace en
    corbeille reste restaurable, pas adressable : créer une discussion dedans
    promettreait un lieu que l'interface d'espace n'affichera pas.
    """
    from backend.db.database import SessionLocal
    from backend.services import espaces as service

    db = SessionLocal()
    try:
        return (
            service.get_workspace(db, workspace_id, include_trashed=False) is not None
        )
    except Exception as exc:
        log(
            f"[workspace] vérification impossible pour {workspace_id}: {_safe_public_text(exc)}"
        )
        return False
    finally:
        try:
            db.close()
        except Exception:
            pass


def _authorized_attachment_ids(conv_id: str, attachment_ids: list[str]) -> list[str]:
    if not attachment_ids:
        return []
    from backend.db.database import SessionLocal
    from backend.db.models import Artifact, ArtifactLink

    db = SessionLocal()
    try:
        authorized: list[str] = []
        for artifact_id in attachment_ids:
            artifact = db.query(Artifact).filter(Artifact.id == artifact_id).first()
            link = (
                db.query(ArtifactLink)
                .filter(
                    ArtifactLink.artifact_id == artifact_id,
                    ArtifactLink.scope_kind == "conversation",
                    ArtifactLink.scope_id == conv_id,
                    ArtifactLink.removed_at.is_(None),
                )
                .first()
            )
            if (
                artifact is not None
                and link is not None
                and artifact.deleted_at is None
            ):
                authorized.append(artifact_id)
            else:
                log(f"[scope] attachment {artifact_id} hors scope {conv_id}")
        return authorized
    finally:
        db.close()


def _safe_public_text(value: object) -> str:
    try:
        from backend.services.flux_spike import sanitize_local_paths

        return sanitize_local_paths(value)
    except Exception:
        return "[erreur locale]"


MESSAGE_RESSOURCE_OCCUPEE = (
    "Le moteur était occupé par une tâche de fond (ingestion d'un fichier). "
    "Votre message n'a pas été traité. Réessayez dans un instant."
)
MESSAGE_ENTRETIEN_MEMOIRE = (
    "Un entretien mémoire est en cours. Réessayez dans un instant."
)
MESSAGE_AUTRE_TACHE = (
    "Le moteur est occupé par une autre tâche. Réessayez dans un instant."
)


def _message_reservation_refusee() -> str:
    """Motif exact d'un refus de réservation, selon le travail qui la détient.

    Le code reste `MEMORY_BUSY` (contrat frontend historique). Seul le texte
    doit dire la vérité : un chat ou une image qui tient le verrou n'est pas un
    entretien mémoire.
    """
    from backend.services.activity import etat_activite

    if etat_activite()["kind"] == "memoire":
        return MESSAGE_ENTRETIEN_MEMOIRE
    return MESSAGE_AUTRE_TACHE


def _message_resource_busy(_exc: object) -> str:
    """Message public d'une contention de ressource, sans détail technique.

    `RESOURCE_BUSY:model` est un marqueur interne : l'utilisateur n'a rien à en
    faire, et il ne dit pas que le tour est retryable. Le détail (`str(exc)`)
    reste dans le journal, où il sert au diagnostic.
    """
    return MESSAGE_RESSOURCE_OCCUPEE


def _code_de_refus(message: str) -> str:
    """Le code machine d'un refus d'existence, d'après le marqueur du service.

    Les services marquent leurs refus (`[WORKSPACE_NOT_FOUND]`, `[LINK_NOT_FOUND]`)
    pour que la cause reste lisible dans les journaux. On la récupère ici pour
    qu'elle le soit aussi du côté de l'interface : un code stable permet à l'écran
    de distinguer « cet espace est en corbeille » d'une panne, sans jamais
    déchiffrer un texte.
    """
    marqueur = re.match(r"\s*\[([A-Z0-9_]+)\]", message)
    if marqueur:
        return marqueur.group(1).lower()
    return "not_found"


def _replayable_artifact_id(db, artifact_id: str | None) -> str | None:
    if not artifact_id:
        return None
    from backend.db.models import Artifact, Blob

    artifact = db.query(Artifact).filter(Artifact.id == artifact_id).first()
    if artifact is None or artifact.deleted_at is not None or artifact.kind != "image":
        return None
    blob = db.query(Blob).filter(Blob.id == artifact.blob_id).first()
    if blob is None or blob.state not in (None, "available"):
        return None
    return artifact.id


def _published_artifact_id(
    db, run_id: str, output_artifact_id: str | None
) -> str | None:
    direct = _replayable_artifact_id(db, output_artifact_id)
    if direct is not None:
        return direct
    from backend.db.models import ArtifactCard

    cards = (
        db.query(ArtifactCard)
        .order_by(ArtifactCard.created_at.asc(), ArtifactCard.id.asc())
        .all()
    )
    for card in cards:
        try:
            summary = json.loads(card.summary)
        except (TypeError, ValueError):
            continue
        if not isinstance(summary, dict) or summary.get("run_id") != run_id:
            continue
        artifact_id = _replayable_artifact_id(db, card.artifact_id)
        if artifact_id is not None:
            return artifact_id
    return None


def persistance_queued(
    run_id: str,
    conv_id: str,
    kind: str = "chat",
    fingerprint: str | None = None,
) -> None:
    """Un Run soumis est écrit EN FILE (`queued`) avant toute exécution.

    orchestration.md §2.1 : « La file d'attente est conservée par le backend,
    qui retient l'identité et la destination de chaque demande en attente. Son
    passage à l'exécution vérifie à nouveau la discussion, les fichiers, les
    permissions et la disponibilité courants. » La persistance est donc écrite
    dès l'admission, à côté de la validation C11, pour que l'état d'un Run
    puisse survivre à un redémarrage ET rester visible par l'interface via le
    chemin d'état existant (aucun nouveau verbe).

    LIM-011 reste INCHANGÉE ici : « l'ordre exact de la file, les priorités, les
    bornes et les actions d'annulation restent à spécifier » (orchestration.md
    §2.1). Cette fonction ne tranche RIEN sur l'ordonnancement ; elle enregistre
    la file, pas la politique d'enchaînement.

    Pourquoi ouvrir sa propre session : le code appelant continue ensuite avec
    sa propre session déjà ouverte — une écriture à l'intérieur risquerait de
    laisser une transaction plus longue que nécessaire. La file doit être
    déjà validée au démarrage de l'exécution : l'écriture est donc passée et
    terminée AVANT l'ouverture du tour.
    """
    try:
        from backend.db.database import SessionLocal as _SLQ
        from backend.db.models import Run as _RunQ

        db = _SLQ()
        try:
            row = db.query(_RunQ).filter(_RunQ.id == run_id).first()
            if row is not None:
                return  # ligne déjà là ; l'idempotence C11 a déjà filtré le doublon
            # Le rattachement de la conversation : pour un `conv_id="tmp"`
            # (discussion sans mapping DB), la clé étrangère `runs.conv_id`
            # exige une ligne existante ; sans ligne, on conserve
            # `conv_id=None` plutôt qu'un FK avorté en silence
            # (`foreign_keys=ON` sur la base).
            destination = conv_id if conv_id != "tmp" else None
            if destination:
                from backend.db.models import Conversation as _Conv

                existe = db.query(_Conv).filter(_Conv.id == destination).first()
                if existe is None:
                    destination = None
            db.add(
                _RunQ(
                    id=run_id,
                    conv_id=destination,
                    request_id=run_id,
                    kind=kind,
                    status="queued",
                    fingerprint=fingerprint,
                )
            )
            db.commit()
        except Exception as e:
            log(f"[file] persistance queued a échoué {run_id}: {e}")
            try:
                db.rollback()
            except Exception:
                pass
            try:
                emit(
                    "error",
                    {
                        "request_id": run_id,
                        "code": "run_queued_persist_failed",
                        "message": _safe_public_text(str(e))[:300],
                        "retryable": True,
                    },
                    run_id=run_id,
                )
            except Exception:
                pass  # best effort : l'échec réel remonte via le raise
            raise RuntimeError("[PERSISTENCE_QUEUED_ECHEC] " + str(e)) from e
        finally:
            try:
                db.close()
            except Exception:
                pass
    except RuntimeError:
        raise
    except Exception as e:
        log(f"[file] persistance queued indisponible {run_id}: {e}")
        raise RuntimeError("[PERSISTENCE_QUEUED_ECHEC] " + str(e)) from e
    return


def _set_run_status(run_id: str, status: str, artifact_id: str | None = None) -> None:
    try:
        from backend.db.database import SessionLocal
        from backend.db.models import Run

        db = SessionLocal()
        try:
            row = db.query(Run).filter(Run.id == run_id).first()
            if row is not None:
                row.status = status
                if artifact_id:
                    row.output_artifact_id = artifact_id
                db.commit()
        finally:
            db.close()
    except Exception as exc:
        log(f"[db] run status update failed: {_safe_public_text(exc)}")


def _emit_artifact_event(
    db,
    *,
    request_id: str,
    run_id: str,
    artifact_id: str,
    conversation_id: str | None = None,
    deduplicated: bool = False,
) -> bool:
    from backend.services.effect_ledger import (
        artifact_event_key,
        mark_event_published_by_key,
    )

    delivered = emit(
        "artifact_created",
        {
            "request_id": request_id,
            "run_id": run_id,
            "artifact_id": artifact_id,
            "experimental": True,
            "deduplicated": deduplicated,
        },
        run_id=run_id,
        conversation_id=conversation_id,
    )
    if delivered is not False:
        mark_event_published_by_key(db, artifact_event_key(run_id, artifact_id))
    return delivered


def _replay_pending_artifact_events() -> None:
    try:
        from backend.db.database import SessionLocal
        from backend.services.effect_ledger import (
            discard_event,
            is_artifact_replayable,
            mark_event_published,
            pending_events,
        )

        db = SessionLocal()
        try:
            for event in pending_events(db, limit=100):
                if event.event_type != "artifact_created":
                    continue
                payload = dict(event.payload or {})
                artifact_id = str(payload.get("artifact_id") or "")
                if not artifact_id or not is_artifact_replayable(
                    db, artifact_id, payload.get("conversation_id")
                ):
                    discard_event(db, event.id)
                    continue
                payload.setdefault("request_id", event.run_id or "")
                delivered = emit(
                    "artifact_created",
                    payload,
                    run_id=event.run_id,
                    conversation_id=payload.get("conversation_id"),
                )
                if delivered is not False:
                    mark_event_published(db, event.id)
        finally:
            db.close()
    except Exception as exc:
        log(f"[outbox] replay failed: {_safe_public_text(exc)}")


def _persist_assistant_trace(
    db, conv_id: str, content: str, status: str, run_id: str, trace: dict
) -> None:
    """Persiste le message assistant + trace orchestrateur + backfill toolcalls.

    Une seule transaction appelante (commit dehors) : message (brief/plan/thinking)
    puis UPDATE toolcalls SET msg_id WHERE run_id — le tout AVANT le terminal (C08).
    """
    from backend.db.repositories.conversations import append_message

    try:
        plan = (trace or {}).get("plan")
        plan_json = json.dumps(plan, ensure_ascii=False) if plan else None
    except Exception:
        plan_json = None
    msg = append_message(
        db,
        conversation_id=conv_id,
        role="assistant",
        content=content,
        status=status,
        run_id=run_id,
        orchestrator_thinking=((trace or {}).get("orchestrator_thinking") or "")[:4000]
        or None,
        brief=((trace or {}).get("brief") or "")[:6000] or None,
        plan_json=plan_json,
    )
    msg.brief_json = (trace or {}).get("brief_json") or None
    try:
        from backend.db.models import MessageAttachment, Run, ToolCall

        db.query(ToolCall).filter(
            ToolCall.run_id == run_id, ToolCall.msg_id.is_(None)
        ).update({"msg_id": msg.id}, synchronize_session=False)
        run = db.query(Run).filter(Run.id == run_id).first()
        if run is not None:
            run.status = status
            artifact_id = (trace or {}).get("image_artifact_id")
            if artifact_id:
                run.output_artifact_id = str(artifact_id)
                exists = (
                    db.query(MessageAttachment)
                    .filter(
                        MessageAttachment.message_id == msg.id,
                        MessageAttachment.artifact_id == str(artifact_id),
                    )
                    .first()
                )
                if exists is None:
                    db.add(
                        MessageAttachment(
                            id=f"matt_{msg.id}_{str(artifact_id)[:24]}",
                            message_id=msg.id,
                            artifact_id=str(artifact_id),
                            ordinal=0,
                        )
                    )
    except Exception as e:
        log(f"[db] assistant trace finalization failed: {e}")
        raise


# Commandes anciennement en rusqlite cote Electron, portees ici. Le nom de methode
# de protocole suit la convention `verbe.nom` ; l'evenement de reponse reprend le
# nom avec des tirets bas (`list.conversations` -> `list_conversations`), ce que le
# pont Electron attend.
_IPC_HANDLERS: dict[str, str] = {
    "list.conversations": "backend.ipc.base:list_conversations",
    "get.conversation": "backend.ipc.base:get_conversation",
    "list.messages": "backend.ipc.base:list_messages",
    "rename.conversation": "backend.ipc.base:rename_conversation",
    "delete.conversation": "backend.ipc.base:delete_conversation",
    "image.list": "backend.ipc.images:image_list",
    "get.attachment_bytes": "backend.ipc.attachments:get_attachment_bytes",
    "save.upload": "backend.ipc.attachments:save_upload",
    "stage.paths": "backend.ipc.attachments:stage_dropped_paths",
    "stage.picker_bytes": "backend.ipc.attachments:stage_picker_bytes",
    "attachments.prepare": "backend.ipc.attachments:attachments_prepare",
    "artifact.ingestion_retry": "backend.ipc.attachments:artifact_ingestion_retry",
    "get.staged_preview_bytes": "backend.ipc.attachments:get_staged_preview_bytes",
    "image.delete": "backend.ipc.images:image_delete",
    "image.rename": "backend.ipc.images:image_rename",
    "image.set_backend": "backend.ipc.images:image_set_backend",
    "espace.list": "backend.ipc.espaces:espace_list",
    "espace.create": "backend.ipc.espaces:espace_create",
    "espace.get": "backend.ipc.espaces:espace_get",
    "espace.stats": "backend.ipc.espaces:espace_stats",
    "espace.conversations": "backend.ipc.espaces:espace_conversations",
    "espace.memoire": "backend.ipc.espaces:espace_memoire",
    "espace.memoire.rafraichir": "backend.ipc.espaces:espace_memoire_rafraichir",
    "espace.memoire.forcer": "backend.ipc.espaces:espace_memoire_forcer",
    "espace.memoire.corriger": "backend.ipc.espaces:espace_memoire_corriger",
    "espace.memoire.supprimer": "backend.ipc.espaces:espace_memoire_supprimer",
    "espace.depot": "backend.ipc.espaces:espace_depot",
    "espace.depot_retirer": "backend.ipc.espaces:espace_depot_retirer",
    "espace.depot_integrite": "backend.ipc.espaces:espace_depot_verifier",
    "espace.depot_reparer": "backend.ipc.espaces:espace_depot_reparer",
    "espace.conversation_deplacer": "backend.ipc.espaces:espace_conversation_deplacer",
    "espace.conversation_partager": "backend.ipc.espaces:espace_conversation_partager",
    "espace.depot_renommer": "backend.ipc.espaces:espace_depot_renommer",
}


def _resolve_ipc(target: str):
    module_name, _, attr = target.partition(":")
    from importlib import import_module

    return getattr(import_module(module_name), attr)


def _dispatch_ipc(
    method: str, params: dict, request_id: str, run_id: str | None
) -> None:
    """Ouvre une session, appelle le handler, emet la reponse ou l'echec.

    Une erreur metier (ValueError : id inconnu, titre vide) devient un refus
    lisible ; une erreur technique (RuntimeError : base injoignable) est bornee
    a 300 caracteres pour ne jamais renvoyer de trace Python au frontend.
    """
    from backend.db.database import SessionLocal

    handler = _resolve_ipc(_IPC_HANDLERS[method])
    event = method.replace(".", "_")
    db = SessionLocal()
    try:
        payload = handler(db, params if isinstance(params, dict) else {})
    except ValueError as exc:
        emit(
            "error",
            {
                "request_id": request_id,
                "code": "bad_request",
                "message": _safe_public_text(exc)[:300],
                "retryable": False,
            },
            run_id=run_id,
        )
        return
    except LookupError as exc:
        # Un refus d'existence n'est pas une panne : le rejouer donnerait le même
        # refus. Le laisser passer en `backend_error` ferait dire « réessaye » à
        # l'utilisateur pour un dépôt dans un espace en corbeille, ce qui est
        # faux et l'enverrait dans une boucle. Les services marquent déjà leurs
        # refus (`[WORKSPACE_NOT_FOUND]`, `[LINK_NOT_FOUND]`) : ce marqueur devient
        # le code transmis, et le texte reste le même qu'ailleurs.
        emit(
            "error",
            {
                "request_id": request_id,
                "code": _code_de_refus(str(exc)),
                "message": _safe_public_text(exc)[:300],
                "retryable": False,
            },
            run_id=run_id,
        )
        return
    except Exception as exc:  # noqa: BLE001 - une trace technique ne sort jamais
        log(f"[ipc] {method} a echoue: {_safe_public_text(exc)}")
        emit(
            "error",
            {
                "request_id": request_id,
                "code": "backend_error",
                "message": _safe_public_text(exc)[:300],
                "retryable": True,
            },
            run_id=run_id,
        )
        return
    finally:
        try:
            db.close()
        except Exception:
            pass
    emit(event, {"request_id": request_id, **payload}, run_id=run_id)


def handle(normalized: dict):
    """
    Handler C08 : reçoit {request_id, method, params, protocol_version}
    Répond via emit() corrélé run_id/seq. Un seul terminal par run après persistance.
    """
    request_id = normalized.get("request_id")
    if not isinstance(request_id, str) or not is_valid_request_id(request_id):
        emit(
            "error",
            {
                "request_id": "unknown",
                "code": "invalid_request_id",
                "message": "request_id invalide",
                "retryable": False,
            },
            run_id=None,
        )
        return
    method = normalized["method"]
    params = normalized["params"]
    proto = normalized.get("protocol_version", 1)

    # Corrélation : run_id = request_id pour chat.send (Phase 1 simplifié)
    # En phase 5, run_id distinct, mais ici 1:1 pour idempotence simple C11
    run_id = request_id

    if method == "chat.send":
        from backend.services.activity import liberer, try_reserver

        reservation = try_reserver("chat", f"chat:{run_id}")
        if reservation is None:
            emit(
                "error",
                {
                    "request_id": request_id,
                    "code": "MEMORY_BUSY",
                    "message": _message_reservation_refusee(),
                    "retryable": True,
                },
                run_id=run_id,
                conversation_id=None,
            )
            return
        try:
            message = (params.get("message") or "").strip()
            # Annexe 10 : attachment_ids durables (cible), image_paths compat déprécié
            image_paths = params.get("image_paths") or params.get("image") or []
            if isinstance(image_paths, str):
                image_paths = [image_paths]
            image_paths = [p for p in image_paths if p]
            attachment_ids = (
                params.get("attachment_ids") or params.get("attachmentIds") or []
            )
            if isinstance(attachment_ids, str):
                attachment_ids = [attachment_ids]
            attachment_ids = [a for a in attachment_ids if a]
            # Espace d'appartenance demandé (camelCase accepté comme attachment_ids).
            # Lu une fois ici : il ne sert qu'à la création de la conversation, et ne
            # doit jamais filer plus loin — la portée d'un chat se déduit du serveur.
            _raw_workspace_id = params.get("workspace_id")
            if _raw_workspace_id is None:
                _raw_workspace_id = params.get("workspaceId")
            conv_id = (
                params.get("conv_id")
                or params.get("conversation_id")
                or request_id
                or "tmp"
            ).strip()
            if not conv_id:
                conv_id = request_id
            # Un espace demandé mais illisible est une erreur, pas une absence : on
            # refuse bruyamment plutôt que de créer un chat simple en silence, ce qui
            # ferait retomber la discussion promise hors de son espace.
            if _raw_workspace_id is not None and not isinstance(_raw_workspace_id, str):
                emit(
                    "error",
                    {
                        "request_id": request_id,
                        "code": "invalid_workspace_id",
                        "message": "Identifiant d'espace invalide",
                        "retryable": False,
                    },
                    run_id=run_id,
                    conversation_id=conv_id,
                )
                return
            workspace_id = (
                _raw_workspace_id.strip() if isinstance(_raw_workspace_id, str) else ""
            )
            # Espace inconnu ou en corbeille : refus avant toute écriture, sinon la
            # discussion promise à l'écran atterrirait hors de l'espace demandé.
            if workspace_id and not _espace_actif_connu(workspace_id):
                emit(
                    "error",
                    {
                        "request_id": request_id,
                        "code": "workspace_not_found",
                        "message": "Espace introuvable ou en corbeille",
                        "retryable": False,
                    },
                    run_id=run_id,
                    conversation_id=conv_id,
                )
                return
            # La demande d'annulation est revérifiée APRÈS l'acquisition de la
            # réservation : un `cancel` arrivé entre l'admission et le début du
            # tour ne doit pas lancer la boucle pour rien.
            cancel_flag = get_cancel_flag(run_id)
            if cancel_flag.is_set():
                clear_cancel_flag(run_id)
                emit(
                    "run_cancelled",
                    {"request_id": request_id, "run_id": run_id},
                    run_id=run_id,
                    conversation_id=conv_id,
                )
                emit(
                    "message_completed",
                    {"request_id": request_id, "run_id": run_id, "cancelled": True},
                    run_id=run_id,
                    conversation_id=conv_id,
                )
                return
            from pathlib import Path as _Path
            from backend.config.paths import get_temp_dir as _get_temp_dir

            _temp_root = _get_temp_dir().resolve()
            _safe_image_paths: list[str] = []
            for _raw_path in image_paths:
                try:
                    _resolved_path = _Path(_raw_path).resolve()
                except Exception:
                    continue
                if (
                    _resolved_path != _temp_root
                    and _temp_root not in _resolved_path.parents
                ):
                    log(f"[scope] image_paths hors staging géré: {_raw_path}")
                    continue
                if _resolved_path.exists():
                    _safe_image_paths.append(str(_resolved_path))
            image_paths = _safe_image_paths
            attachment_ids = _authorized_attachment_ids(conv_id, attachment_ids)
            # Compat migration : image_paths dans staging géré → promeut en artifact_id et log
            if image_paths and not attachment_ids:
                log(
                    f"[compat] image_paths reçu ({len(image_paths)}), promotion via staging"
                )
                # Tente promotion via même logique que attachments.prepare (hash → blob)
                try:
                    from backend.db.database import SessionLocal as _SL
                    from backend.db.models import Blob, Artifact, ArtifactLink
                    import hashlib, pathlib

                    _dbp = _SL()
                    for p in image_paths:
                        # Vérifie que chemin est dans temp/staged ou temp/{request_id} géré
                        from backend.config.paths import get_temp_dir

                        temp = get_temp_dir()
                        if (
                            temp not in pathlib.Path(p).resolve().parents
                            and pathlib.Path(p).resolve().parent != temp
                        ):
                            # Hors staging géré → refus C18, mais on tolère en compat avec log
                            log(f"[compat] image_paths hors staging géré: {p}")
                        # Calcule hash et promeut si possible (similaire à attachments_prepare)
                        try:
                            data = (
                                pathlib.Path(p).read_bytes()
                                if pathlib.Path(p).exists()
                                else b""
                            )
                            sha = hashlib.sha256(data).hexdigest() if data else None
                            if sha:
                                # Vérifie si déjà promu
                                existing = (
                                    _dbp.query(Artifact)
                                    .filter(Artifact.id == f"art_{sha[:12]}")
                                    .first()
                                )
                                if not existing:
                                    # Crée blob/artifact minimal pour compat
                                    from pathlib import Path as _P

                                    sha_path = (
                                        _dbp.query(Blob)
                                        .filter(Blob.sha256 == sha)
                                        .first()
                                    )
                                    if not sha_path:
                                        rel = f"blobs/{sha[:2]}/{sha}.bin"
                                        _dbp.add(
                                            Blob(
                                                id=sha,
                                                sha256=sha,
                                                relative_path=rel,
                                                size_bytes=len(data),
                                                mime_detected="image/png",
                                                state="available",
                                            )
                                        )
                                        _dbp.flush()
                                    # Crée artifact
                                    art_id = f"art_{sha[:12]}"
                                    _dbp.add(
                                        Artifact(
                                            id=art_id,
                                            blob_id=sha,
                                            kind="image",
                                            original_filename=pathlib.Path(p).name,
                                            display_name=pathlib.Path(p).name,
                                        )
                                    )
                                    _dbp.flush()
                                    _dbp.add(
                                        ArtifactLink(
                                            id=f"lnk_{art_id[4:8]}_{request_id[:8]}",
                                            artifact_id=art_id,
                                            scope_kind="conversation",
                                            scope_id=conv_id
                                            if "conv_id" in locals()
                                            else "tmp",
                                        )
                                    )
                                    _dbp.commit()
                                    # Annexe 11 Lot A : fiche rev 1 dès la promotion (sinon self-healing au run)
                                    try:
                                        from backend.search import (
                                            cards as _cardsP,
                                            fts as _ftsP,
                                        )

                                        _artP = (
                                            _dbp.query(Artifact)
                                            .filter(Artifact.id == art_id)
                                            .first()
                                        )
                                        if _artP is not None:
                                            _ftsP.ensure_fts(_dbp)
                                            _cardsP.ensure_card_on_import(_dbp, _artP)
                                            _dbp.commit()
                                    except Exception as _eP:
                                        log(
                                            f"[compat] fiche Lot A ignorée {art_id}: {_eP}"
                                        )
                                attachment_ids.append(f"art_{sha[:12]}")
                        except Exception as e:
                            log(f"[compat] promotion échouée {p}: {e}")
                    _dbp.close()
                except Exception as e:
                    log(f"[compat] promotion image_paths échouée: {e}")

            attachment_ids = _authorized_attachment_ids(conv_id, attachment_ids)

            # Validation entrée C18
            if not message and not image_paths and not attachment_ids:
                emit(
                    "error",
                    {
                        "request_id": request_id,
                        "code": "empty_message",
                        "message": "Message vide",
                        "retryable": False,
                    },
                    run_id=run_id,
                    conversation_id=conv_id,
                )
                return
            # Validation Taille
            if len(message) > 200_000:
                emit(
                    "error",
                    {
                        "request_id": request_id,
                        "code": "message_too_large",
                        "message": "Message trop volumineux",
                        "retryable": False,
                    },
                    run_id=run_id,
                    conversation_id=conv_id,
                )
                return

            # Idempotence C11 : si run_id déjà terminé et même empreinte → rejouer acceptation sans réexécuter
            fingerprint = _canonical_fingerprint(method, params, proto)
            # Vérifie en DB si message user avec run_id existe déjà
            from backend.db.database import SessionLocal

            db_check = SessionLocal()
            try:
                from backend.db.models import Message, Run

                run_row = db_check.query(Run).filter(Run.id == run_id).first()
                if run_row is not None:
                    if run_row.fingerprint != fingerprint or (
                        run_row.conv_id and run_row.conv_id != conv_id
                    ):
                        emit(
                            "error",
                            {
                                "request_id": request_id,
                                "code": "idempotency_mismatch",
                                "message": "request_id déjà utilisé avec des paramètres différents",
                                "retryable": False,
                            },
                            run_id=run_id,
                            conversation_id=conv_id,
                        )
                        return
                existing = (
                    db_check.query(Message).filter(Message.run_id == run_id).first()
                )
                if existing is not None:
                    # Même run_id déjà traité → dedup, vérifier empreinte via content hash
                    # Simplification : si message identique, rejouer le terminal depuis DB
                    log(
                        f"[dedup] run_id {run_id} déjà existant, restitution sans réexécution"
                    )
                    # Récupère réponse assistant associée
                    assistant = (
                        db_check.query(Message)
                        .filter(
                            Message.conv_id == conv_id,
                            Message.role == "assistant",
                            Message.run_id == run_id,
                        )
                        .first()
                    )
                    if assistant:
                        # Émet idempotent : run.started + answer + run.completed depuis persistant
                        emit(
                            "run_started",
                            {"request_id": request_id, "run_id": run_id},
                            run_id=run_id,
                            conversation_id=conv_id,
                        )
                        emit(
                            "answer_delta",
                            {
                                "request_id": request_id,
                                "text": assistant.content[:4000],
                                "done": True,
                            },
                            run_id=run_id,
                            conversation_id=conv_id,
                        )
                        emit(
                            "run_completed",
                            {
                                "request_id": request_id,
                                "run_id": run_id,
                                "status": "completed",
                            },
                            run_id=run_id,
                            conversation_id=conv_id,
                        )
                        # Compat legacy
                        emit(
                            "message_started",
                            {"request_id": request_id, "run_id": run_id},
                            run_id=run_id,
                            conversation_id=conv_id,
                        )
                        emit(
                            "answer",
                            {
                                "request_id": request_id,
                                "text": assistant.content[:4000],
                            },
                            run_id=run_id,
                            conversation_id=conv_id,
                        )
                        emit(
                            "message_completed",
                            {"request_id": request_id, "run_id": run_id},
                            run_id=run_id,
                            conversation_id=conv_id,
                        )
                        return
            except Exception as e:
                log(f"[dedup] check failed: {e}")
            finally:
                try:
                    db_check.close()
                except:
                    pass

            # Vérification atomique 1 run chat non-terminal par conversation C09
            # Simplification Phase 1 : on vérifie qu'aucun run en cours pour cette conv (flag en mémoire)
            # + en DB on vérifie qu'aucun message user récent non terminé ? Pour l'instant lock mémoire
            # On utilise un lock global par conv_id
            # Pour rester simple, on refuse si un flag cancel existe déjà pour cette conv
            # (vrai verrou DB nécessiterait table Run avec état)

            from backend.db.database import SessionLocal

            db = SessionLocal()
            history_shared: list[dict] = []
            try:
                from backend.db.repositories.conversations import (
                    get_orchestrator_context,
                    get_conversation,
                    create_conversation,
                    append_message,
                )

                conv = get_conversation(db, conv_id)
                if conv is None and conv_id != "tmp":
                    title = (message or "Nouvelle conversation")[
                        :80
                    ].strip() or "Nouvelle conversation"
                    try:
                        conv = create_conversation(
                            db,
                            title=title,
                            conv_id=conv_id,
                            workspace_id=workspace_id or None,
                        )
                        db.commit()
                    except Exception as e:
                        log(f"[db] create_conversation failed: {e}")
                        db.rollback()
                if conv is not None:
                    history_shared = get_orchestrator_context(db, conv_id)
                # Persiste message user immédiatement avant LLM (survit crash) C11
                user_msg_id = None
                if message and conv is not None:
                    try:
                        # Idempotence : si même run_id déjà, ne pas réinsérer
                        from backend.db.models import Message

                        if (
                            not db.query(Message)
                            .filter(Message.run_id == run_id, Message.role == "user")
                            .first()
                        ):
                            m = append_message(
                                db,
                                conversation_id=conv_id,
                                role="user",
                                content=message,
                                status="completed",
                                run_id=run_id,
                            )
                            user_msg_id = m.id
                            # Annexe 10 : persistance via artifact_ids durables (cible).
                            # D'OÙ VIENNENT LES IDS : frontend ChatConversation.handleSend → attachmentsPrepare
                            # (Electron promeut sti_xxx → art_xxx) → chatSend(attachment_ids). OÙ VONT-ILS :
                            # MessageAttachment(message, artifact) = "ce message montre ce fichier" → lu par
                            # loop.build_history_artifact_entries pour les follow-ups (registre historique).
                            # POURQUOI PAS Source (legacy) : Source servait l'ancien pipeline image_paths ;
                            # le pipeline durable écrit MessageAttachment + ArtifactLink. Les lecteurs
                            # historiques (registre, ask_files) connaissent les DEUX tables (compat).
                            if attachment_ids:
                                from backend.db.models import (
                                    MessageAttachment,
                                    Artifact,
                                    ArtifactLink,
                                )
                                import uuid

                                for idx, art_id in enumerate(attachment_ids):
                                    try:
                                        # Vérifie scope conversation (C18) : l'artefact a-t-il été préparé
                                        # POUR cette conv ? D'où vient le link : attachments_prepare (Electron) le crée
                                        # avec scope_id = conv_id du frontend. Pourquoi refuser sinon : sans ça, un
                                        # artifact_id deviné/collé donnerait accès aux fichiers d'une AUTRE
                                        # conversation (fuite inter-conv). Le refus est un log, pas une erreur :
                                        # le run continue (texte seul) au lieu de mourir en toast.
                                        link = (
                                            db.query(ArtifactLink)
                                            .filter(
                                                ArtifactLink.artifact_id == art_id,
                                                ArtifactLink.scope_kind
                                                == "conversation",
                                                ArtifactLink.scope_id == conv_id,
                                                ArtifactLink.removed_at.is_(None),
                                            )
                                            .first()
                                        )
                                        if not link:
                                            log(
                                                f"[scope] attachment {art_id} hors scope {conv_id} — refus C18"
                                            )
                                            continue
                                        # Vérifie artifact existe
                                        art = (
                                            db.query(Artifact)
                                            .filter(Artifact.id == art_id)
                                            .first()
                                        )
                                        if not art:
                                            log(f"[artifact] {art_id} introuvable")
                                            continue
                                        db.add(
                                            MessageAttachment(
                                                id=str(uuid.uuid4()),
                                                message_id=m.id,
                                                artifact_id=art_id,
                                                ordinal=idx,
                                            )
                                        )
                                    except Exception as e:
                                        log(f"[msg_att] failed {art_id}: {e}")
                            # Compat image_paths (déprécié) → legacy Source pour migration
                            if image_paths:
                                from backend.db.media_cache import persist_attachment
                                from backend.db.models import Source
                                import mimetypes, uuid

                                for p in image_paths:
                                    try:
                                        rel, sha256, size = persist_attachment(p)
                                        src = Source(
                                            id=str(uuid.uuid4()),
                                            msg_id=m.id,
                                            conversation_id=conv_id,
                                            source_type="attachment",
                                            path=p,
                                            hash=sha256,
                                            media_kind="pdf"
                                            if p.lower().endswith(".pdf")
                                            else "image",
                                            original_name=Path(p).name,
                                            mime_type=mimetypes.guess_type(p)[0]
                                            or (
                                                "application/pdf"
                                                if p.lower().endswith(".pdf")
                                                else "image/png"
                                            ),
                                            sha256=sha256,
                                            relative_path=rel,
                                            size_bytes=size,
                                            analysis_version=0,
                                        )
                                        db.add(src)
                                    except Exception as e:
                                        log(f"[media] persist failed {p}: {e}")
                            db.commit()
                        else:
                            log(f"[dedup] user message {run_id} déjà persisté")
                        # Activité à la RÉCEPTION du message : le compteur de tours,
                        # lui, n'avancera qu'après un tour réussi.
                        try:
                            from backend.services import memoire_auto as _memoire_auto

                            _memoire_auto.enregistrer_activite_espace(
                                db, conv_id, workspace_id or None
                            )
                        except Exception as activity_error:
                            log(f"[memoire] activité non enregistrée: {activity_error}")
                    except Exception as e:
                        log(f"[db] append user failed: {e}")
                        db.rollback()
            except Exception as e:
                log(f"[db] history load failed: {e}")
                history_shared = []
            finally:
                try:
                    db.close()
                except:
                    pass

            # File d'attente persistée (orchestration.md §2.1, tâche 5.2) : un Run
            # soumis est d'abord écrit `queued`, AVANT toute exécution — l'état
            # survit à un redémarrage, et au passage à l'exécution
            # `_ensure_chat_run` re-vérifie discussion, fichiers et disponibilité.
            persistance_queued(
                run_id,
                conv_id,
                kind="chat",
                fingerprint=fingerprint,
            )

            # Émet acceptation + début de run (C08 : chat.send retourne acceptation, stream ensuite)
            emit(
                "run_started",
                {
                    "request_id": request_id,
                    "run_id": run_id,
                    "conversation_id": conv_id,
                    "fingerprint": fingerprint,
                },
                run_id=run_id,
                conversation_id=conv_id,
            )
            emit(
                "message_started",
                {"request_id": request_id, "run_id": run_id, "model": "LFM2.5-8B"},
                run_id=run_id,
                conversation_id=conv_id,
            )  # compat
            # Flag annulation
            cancel_flag = get_cancel_flag(run_id)

            # Exécution boucle (bloquant dans worker, pas dans lecteur)
            collected = ""
            # Trace orchestrateur (bulle UI) : (contenus itérations, brief, plan) — persistée AVANT terminal
            run_trace: dict = {"orchestrator_thinking": "", "brief": "", "plan": None}
            error_occurred = False
            try:
                from backend.inference.loop import run_agent_loop

                # La réservation `chat` tient déjà TURN_ACTIVE : le planificateur
                # mémoire consulte ce drapeau et ne travaille jamais pendant un tour.

                # Passe cancel_flag pour arrêt coopératif C09, attachment_ids durables annexe 10
                collected, run_trace = run_agent_loop(
                    manager,
                    conv_id=conv_id,
                    request_id=request_id,
                    message=message,
                    image_paths=image_paths,
                    attachment_ids=attachment_ids,
                    history_shared=history_shared,
                    cancel_flag=cancel_flag,
                    runtime=get_chat_runtime(),
                    request_fingerprint=fingerprint,
                )
                # Vérifie annulation
                if cancel_flag.is_set() and not run_trace.get("image_published"):
                    log(f"[run] {run_id} annulé")
                    # Persiste état cancelled si pas déjà
                    db2 = SessionLocal()
                    try:
                        from backend.db.repositories.conversations import append_message

                        # Marque message partiel si collected
                        status = "cancelled" if not collected else "partial"
                        if collected:
                            _persist_assistant_trace(
                                db2, conv_id, collected, status, run_id, run_trace
                            )
                            db2.commit()
                    except Exception as e:
                        log(f"[db] append cancelled failed: {e}")
                    finally:
                        try:
                            db2.close()
                        except:
                            pass
                    emit(
                        "run_cancelled",
                        {"request_id": request_id, "run_id": run_id},
                        run_id=run_id,
                        conversation_id=conv_id,
                    )
                    emit(
                        "message_completed",
                        {"request_id": request_id, "run_id": run_id, "cancelled": True},
                        run_id=run_id,
                        conversation_id=conv_id,
                    )
                    return
            except ResourceBusy as e:
                # Contention de ressource : erreur TRANSITOIRE. Elle est
                # retryable et ne doit pas être confondue avec un échec de
                # boucle générique. Le code spécifique permet au client de
                # réessayer au lieu de traiter la requête comme perdue.
                # Le détail technique (`str(e)`, ex. `RESOURCE_BUSY:model`) va
                # au journal ; l'utilisateur reçoit une cause compréhensible.
                log(f"[loop] ressource occupée {run_id}: {e}")
                message_public = _message_resource_busy(e)
                emit(
                    "error",
                    {
                        "request_id": request_id,
                        "code": "RESOURCE_BUSY",
                        "message": message_public[:800],
                        "retryable": True,
                    },
                    run_id=run_id,
                    conversation_id=conv_id,
                )
                emit(
                    "run_failed",
                    {
                        "request_id": request_id,
                        "run_id": run_id,
                        "code": "RESOURCE_BUSY",
                        "message": message_public[:500],
                        "retryable": True,
                    },
                    run_id=run_id,
                    conversation_id=conv_id,
                )
                error_occurred = True
                _set_run_status(run_id, "failed")
                collected = ""
            except Exception as e:
                log(f"[loop] error {run_id}: {e}")
                emit(
                    "error",
                    {
                        "request_id": request_id,
                        "code": "loop_error",
                        "message": _safe_public_text(e)[:800],
                        "retryable": True,
                    },
                    run_id=run_id,
                    conversation_id=conv_id,
                )
                emit(
                    "run_failed",
                    {
                        "request_id": request_id,
                        "run_id": run_id,
                        "code": "loop_error",
                        "message": _safe_public_text(e)[:500],
                    },
                    run_id=run_id,
                    conversation_id=conv_id,
                )
                error_occurred = True
                _set_run_status(run_id, "failed")
                collected = ""
            finally:
                clear_cancel_flag(run_id)

            # Persiste réponse assistant après génération AVANT terminal C08
            if collected and not error_occurred:
                db2 = SessionLocal()
                try:
                    from backend.db.models import Message

                    if (
                        not db2.query(Message)
                        .filter(Message.run_id == run_id, Message.role == "assistant")
                        .first()
                    ):
                        _persist_assistant_trace(
                            db2, conv_id, collected, "completed", run_id, run_trace
                        )
                        db2.commit()
                        # Compteur de tours SEULEMENT après un tour réussi, et
                        # jamais depuis `message_completed` (émis plusieurs fois).
                        try:
                            from backend.services import memoire_auto as _memoire_auto

                            _memoire_auto.finaliser_tour_reussi(
                                db2, conv_id, workspace_id or None
                            )
                        except Exception as activity_error:
                            log(f"[memoire] tour non compté: {activity_error}")
                    else:
                        log(f"[dedup] assistant {run_id} déjà persisté")
                except Exception as e:
                    log(f"[db] append assistant failed: {e}")
                    try:
                        db2.rollback()
                    except Exception:
                        pass
                    error_occurred = True
                    _set_run_status(run_id, "failed")
                finally:
                    try:
                        db2.close()
                    except Exception:
                        pass
                if error_occurred:
                    emit(
                        "run_failed",
                        {
                            "request_id": request_id,
                            "run_id": run_id,
                            "code": "PERSISTENCE_ERROR",
                            "message": "Réponse non persistée",
                        },
                        run_id=run_id,
                        conversation_id=conv_id,
                    )
                else:
                    if not run_trace.get("answer_emitted"):
                        for index in range(0, len(collected), 400):
                            emit(
                                "answer",
                                {
                                    "request_id": request_id,
                                    "text": collected[index : index + 400],
                                },
                                run_id=run_id,
                                conversation_id=conv_id,
                            )
                        run_trace["answer_emitted"] = True
                    emit(
                        "run_completed",
                        {
                            "request_id": request_id,
                            "run_id": run_id,
                            "status": "completed",
                            "tokens_out": len(collected) // 4,
                        },
                        run_id=run_id,
                        conversation_id=conv_id,
                    )
                    emit(
                        "message_completed",
                        {
                            "request_id": request_id,
                            "run_id": run_id,
                            "tokens_in": len(message) // 4,
                            "tokens_out": len(collected) // 4,
                        },
                        run_id=run_id,
                        conversation_id=conv_id,
                    )
            elif not collected and not error_occurred:
                # Pas de collecte (annulation déjà gérée ou vide)
                _set_run_status(run_id, "failed")
                emit(
                    "run_failed",
                    {
                        "request_id": request_id,
                        "run_id": run_id,
                        "code": "empty_response",
                        "message": "Pas de réponse générée",
                    },
                    run_id=run_id,
                    conversation_id=conv_id,
                )

        finally:
            liberer(reservation)

    elif method == "cancel":
        target_run = params.get("run_id") or params.get("request_id") or request_id
        log(f"[cancel] {target_run} demandé")
        flag = get_cancel_flag(target_run)
        flag.set()
        emit(
            "run_cancelling",
            {
                "request_id": request_id,
                "run_id": target_run,
                "status": "cancelling",
            },
            run_id=target_run,
        )

    elif method == "image.generate":
        from backend.db.database import SessionLocal as _ImageSession
        from backend.db.models import Run as _ImageRun
        from backend.services import image_service as _svc
        from backend.services.activity import liberer, try_reserver

        reservation = try_reserver("image", f"image:{request_id}")
        if reservation is None:
            message_refus = _message_reservation_refusee()
            emit(
                "error",
                {
                    "request_id": request_id,
                    "run_id": run_id,
                    "code": "MEMORY_BUSY",
                    "message": message_refus,
                    "retryable": True,
                },
                run_id=run_id,
                conversation_id=None,
            )
            emit(
                "run_failed",
                {
                    "request_id": request_id,
                    "run_id": run_id,
                    "code": "MEMORY_BUSY",
                    "message": message_refus,
                    "retryable": True,
                },
                run_id=run_id,
                conversation_id=None,
            )
            return
        try:
            cancel_flag = get_cancel_flag(run_id)
            if cancel_flag.is_set():
                clear_cancel_flag(run_id)
                emit(
                    "run_cancelled",
                    {
                        "request_id": request_id,
                        "run_id": run_id,
                        "status": "cancelled",
                        "code": "cancelled",
                    },
                    run_id=run_id,
                    conversation_id=None,
                )
                emit(
                    "error",
                    {
                        "request_id": request_id,
                        "run_id": run_id,
                        "code": "cancelled",
                        "message": "Génération annulée.",
                        "retryable": True,
                    },
                    run_id=run_id,
                    conversation_id=None,
                )
                return

            raw_prompt = params.get("prompt")
            raw_aspect_ratio = (
                params.get("aspect_ratio") or params.get("aspectRatio") or "1:1"
            )
            raw_references = params.get("reference_ids")
            if raw_references is None:
                raw_references = params.get("referenceIds")
            if raw_references is None:
                raw_references = []
            if not isinstance(raw_references, list):
                raw_references = [raw_references]
            raw_references = [str(value or "").strip() for value in raw_references]
            raw_seed = params.get("seed")
            idem = params.get("idempotency_key") or params.get("idempotencyKey") or None
            if isinstance(idem, str):
                idem = idem.strip() or None
            raw_mode = params.get("mode") or None
            raw_source_artifact_id = params.get("sourceArtifactId")
            if raw_source_artifact_id is None:
                raw_source_artifact_id = params.get("source_artifact_id")
            raw_work_token = params.get("workToken")
            if raw_work_token is None:
                raw_work_token = params.get("work_token")
            raw_mask_token = params.get("maskToken")
            if raw_mask_token is None:
                raw_mask_token = params.get("mask_token")
            raw_reference_tokens = params.get("referenceTokens")
            if raw_reference_tokens is None:
                raw_reference_tokens = params.get("reference_tokens")
            if raw_reference_tokens is None:
                raw_reference_tokens = []
            if not isinstance(raw_reference_tokens, list):
                raw_reference_tokens = [raw_reference_tokens]
            raw_reference_tokens = [
                str(v or "").strip()
                for v in raw_reference_tokens
                if str(v or "").strip()
            ]
            raw_work_sha256 = params.get("workSha256")
            if raw_work_sha256 is None:
                raw_work_sha256 = params.get("work_sha256")
            raw_mask_sha256 = params.get("maskSha256")
            if raw_mask_sha256 is None:
                raw_mask_sha256 = params.get("mask_sha256")
            raw_reference_sha256 = params.get("referenceSha256")
            if raw_reference_sha256 is None:
                raw_reference_sha256 = params.get("reference_sha256")
            raw_canvas_width = params.get("canvasWidth")
            if raw_canvas_width is None:
                raw_canvas_width = params.get("canvas_width")
            raw_canvas_height = params.get("canvasHeight")
            if raw_canvas_height is None:
                raw_canvas_height = params.get("canvas_height")
            raw_strength = params.get("strength")
            raw_session_id = params.get("sessionId")
            if raw_session_id is None:
                raw_session_id = params.get("session_id")
            raw_revision = params.get("revision")
            raw_mask_kind = params.get("maskKind")
            if raw_mask_kind is None:
                raw_mask_kind = params.get("mask_kind")
            if isinstance(raw_mask_kind, str):
                _mk = raw_mask_kind.strip()
                raw_mask_kind = _mk.lower() if _mk else None
            raw_source_transform = params.get("sourceTransform")
            if raw_source_transform is None:
                raw_source_transform = params.get("source_transform")
            raw_source_size = params.get("sourceSize")
            if raw_source_size is None:
                raw_source_size = params.get("source_size")
            fingerprint = _image_request_fingerprint(method, params, proto)

            try:
                db = _ImageSession()
            except Exception as exc:
                emit(
                    "run_failed",
                    {
                        "request_id": request_id,
                        "run_id": run_id,
                        "code": "STORAGE_ERROR",
                        "message": f"Impossible d'ouvrir la session du run image : {_safe_public_text(exc)}"[
                            :800
                        ],
                    },
                    run_id=run_id,
                    conversation_id=None,
                )
                emit(
                    "error",
                    {
                        "request_id": request_id,
                        "run_id": run_id,
                        "code": "STORAGE_ERROR",
                        "message": f"Impossible d'ouvrir la session du run image : {_safe_public_text(exc)}"[
                            :800
                        ],
                        "retryable": True,
                    },
                    run_id=run_id,
                    conversation_id=None,
                )
                return
            resuming_existing = False
            try:
                existing = (
                    db.query(_ImageRun)
                    .filter(_ImageRun.request_id == request_id)
                    .order_by(_ImageRun.created_at.asc())
                    .first()
                )
                if existing is not None:
                    if existing.fingerprint not in (None, fingerprint):
                        emit(
                            "error",
                            {
                                "request_id": request_id,
                                "run_id": run_id,
                                "code": "REQUEST_CONFLICT",
                                "message": "request_id déjà utilisé avec des paramètres différents.",
                                "retryable": False,
                            },
                            run_id=run_id,
                            conversation_id=None,
                        )
                        return
                    if existing.fingerprint is None:
                        existing.fingerprint = fingerprint
                    status = existing.status or ""
                    if status == "completed":
                        artifact_id = _published_artifact_id(
                            db, request_id, existing.output_artifact_id
                        )
                        if artifact_id is None:
                            emit(
                                "error",
                                {
                                    "request_id": request_id,
                                    "run_id": run_id,
                                    "code": "RUN_RESULT_MISSING",
                                    "message": "Le run terminé ne référence aucun artefact publié.",
                                    "retryable": False,
                                },
                                run_id=run_id,
                                conversation_id=None,
                            )
                            return
                        if existing.output_artifact_id != artifact_id:
                            existing.output_artifact_id = artifact_id
                            db.commit()
                        _emit_artifact_event(
                            db,
                            request_id=request_id,
                            run_id=run_id,
                            artifact_id=artifact_id,
                            deduplicated=True,
                        )
                        emit(
                            "run_completed",
                            {
                                "request_id": request_id,
                                "run_id": run_id,
                                "status": "completed",
                                "artifact_id": artifact_id,
                                "deduplicated": True,
                            },
                            run_id=run_id,
                            conversation_id=None,
                        )
                        return
                    if status == "interrupted":
                        recovered_artifact_id = _published_artifact_id(
                            db, request_id, existing.output_artifact_id
                        )
                        if recovered_artifact_id is not None:
                            existing.status = "completed"
                            existing.output_artifact_id = recovered_artifact_id
                            db.commit()
                            _emit_artifact_event(
                                db,
                                request_id=request_id,
                                run_id=run_id,
                                artifact_id=recovered_artifact_id,
                                deduplicated=True,
                            )
                            emit(
                                "run_completed",
                                {
                                    "request_id": request_id,
                                    "run_id": run_id,
                                    "status": "completed",
                                    "artifact_id": recovered_artifact_id,
                                    "deduplicated": True,
                                },
                                run_id=run_id,
                                conversation_id=None,
                            )
                            return
                        existing.status = "running"
                        existing.output_artifact_id = None
                        db.commit()
                        resuming_existing = True
                    elif status in {"queued", "running", "cancelling", ""}:
                        emit(
                            "error",
                            {
                                "request_id": request_id,
                                "run_id": run_id,
                                "code": "REQUEST_IN_PROGRESS",
                                "message": "Une exécution non terminale avec ce request_id est déjà active.",
                                "retryable": True,
                            },
                            run_id=run_id,
                            conversation_id=None,
                        )
                        return
                    if not resuming_existing:
                        emit(
                            "error",
                            {
                                "request_id": request_id,
                                "run_id": run_id,
                                "code": "REQUEST_TERMINAL",
                                "message": f"Le request_id est déjà terminal ({status}).",
                                "retryable": False,
                            },
                            run_id=run_id,
                            conversation_id=None,
                        )
                        return
                effect_run = (
                    None
                    if resuming_existing
                    else db.query(_ImageRun)
                    .filter(
                        _ImageRun.fingerprint == fingerprint,
                        _ImageRun.kind == "production",
                    )
                    .order_by(_ImageRun.created_at.asc())
                    .first()
                )
                if effect_run is not None:
                    effect_status = effect_run.status or ""
                    if effect_status in {"queued", "running", "cancelling", ""}:
                        emit(
                            "error",
                            {
                                "request_id": request_id,
                                "run_id": run_id,
                                "code": "REQUEST_IN_PROGRESS",
                                "message": "Une exécution non terminale avec cette empreinte est déjà active.",
                                "retryable": True,
                            },
                            run_id=run_id,
                            conversation_id=None,
                        )
                        return
                    if effect_status == "completed":
                        effect_artifact_id = _published_artifact_id(
                            db, effect_run.request_id, effect_run.output_artifact_id
                        )
                        if effect_artifact_id is None:
                            emit(
                                "error",
                                {
                                    "request_id": request_id,
                                    "run_id": run_id,
                                    "code": "RUN_RESULT_MISSING",
                                    "message": "L'effet idempotent terminé ne référence aucun artefact publié.",
                                    "retryable": False,
                                },
                                run_id=run_id,
                                conversation_id=None,
                            )
                            return
                        db.add(
                            _ImageRun(
                                id=run_id,
                                conv_id=None,
                                request_id=request_id,
                                kind="production",
                                status="completed",
                                fingerprint=fingerprint,
                                output_artifact_id=effect_artifact_id,
                                seq=0,
                            )
                        )
                        db.commit()
                        _emit_artifact_event(
                            db,
                            request_id=request_id,
                            run_id=run_id,
                            artifact_id=effect_artifact_id,
                            deduplicated=True,
                        )
                        emit(
                            "run_completed",
                            {
                                "request_id": request_id,
                                "run_id": run_id,
                                "status": "completed",
                                "artifact_id": effect_artifact_id,
                                "deduplicated": True,
                            },
                            run_id=run_id,
                            conversation_id=None,
                        )
                        return
                if not resuming_existing:
                    db.add(
                        _ImageRun(
                            id=run_id,
                            conv_id=None,
                            request_id=request_id,
                            kind="production",
                            status="running",
                            fingerprint=fingerprint,
                            output_artifact_id=None,
                            seq=0,
                        )
                    )
                    db.commit()
            except Exception as exc:
                try:
                    db.rollback()
                except Exception:
                    pass
                emit(
                    "run_failed",
                    {
                        "request_id": request_id,
                        "run_id": run_id,
                        "code": "STORAGE_ERROR",
                        "message": f"Impossible de persister le run image : {_safe_public_text(exc)}"[
                            :800
                        ],
                    },
                    run_id=run_id,
                    conversation_id=None,
                )
                emit(
                    "error",
                    {
                        "request_id": request_id,
                        "run_id": run_id,
                        "code": "STORAGE_ERROR",
                        "message": f"Impossible de persister le run image : {_safe_public_text(exc)}"[
                            :800
                        ],
                        "retryable": True,
                    },
                    run_id=run_id,
                    conversation_id=None,
                )
                return
            finally:
                try:
                    db.close()
                except Exception:
                    pass

            emit(
                "run_started",
                {
                    "request_id": request_id,
                    "run_id": run_id,
                    "conversation_id": None,
                    "kind": "production",
                    "fingerprint": fingerprint,
                },
                run_id=run_id,
                conversation_id=None,
            )
            cancel_flag = get_cancel_flag(run_id)

            def persist_state(status: str, artifact_id: str | None = None) -> None:
                state_db = _ImageSession()
                try:
                    row = (
                        state_db.query(_ImageRun).filter(_ImageRun.id == run_id).first()
                    )
                    if row is None:
                        raise RuntimeError(f"Run image absent : {run_id}")
                    row.status = status
                    if artifact_id is not None:
                        row.output_artifact_id = artifact_id
                    state_db.commit()
                except Exception:
                    state_db.rollback()
                    raise
                finally:
                    try:
                        state_db.close()
                    except Exception:
                        pass

            def mark_published(state_db, artifact_id: str) -> None:
                row = state_db.query(_ImageRun).filter(_ImageRun.id == run_id).first()
                if row is None:
                    raise RuntimeError(f"Run image absent : {run_id}")
                row.status = "completed"
                row.output_artifact_id = artifact_id
                state_db.flush()

            def failure_code(exc: Exception) -> str:
                match = re.search(r"\[([A-Z][A-Z0-9_]+)\]", str(exc))
                if match:
                    code = match.group(1)
                    return (
                        "cancelled"
                        if code == "CANCELLED"
                        else "image_failed"
                        if code == "FAILED"
                        else code
                    )
                return "image_failed"

            def fail(exc: Exception, fallback_code: str = "image_failed") -> None:
                public_code = failure_code(exc)
                if public_code == "image_failed" and fallback_code != "image_failed":
                    public_code = fallback_code
                status = "cancelled" if public_code == "cancelled" else "failed"
                message = _safe_public_text(exc)[:800]
                try:
                    persist_state(status)
                except Exception as storage_exc:
                    public_code = "STORAGE_ERROR"
                    status = "failed"
                    message = f"Persistance du terminal image impossible : {_safe_public_text(storage_exc)}"[
                        :800
                    ]
                terminal_event = (
                    "run_cancelled" if public_code == "cancelled" else "run_failed"
                )
                emit(
                    terminal_event,
                    {
                        "request_id": request_id,
                        "run_id": run_id,
                        "status": "cancelled"
                        if public_code == "cancelled"
                        else "failed",
                        "code": public_code,
                        "message": message,
                        "retryable": public_code
                        not in {"cancelled", "REQUEST_CONFLICT"},
                    },
                    run_id=run_id,
                    conversation_id=None,
                )
                emit(
                    "error",
                    {
                        "request_id": request_id,
                        "run_id": run_id,
                        "code": public_code,
                        "message": message,
                        "retryable": public_code
                        not in {"cancelled", "REQUEST_CONFLICT"},
                    },
                    run_id=run_id,
                    conversation_id=None,
                )

            try:
                from backend.services.effect_ledger import (
                    EffectConflict,
                    EffectInProgress,
                    claim_image_effect,
                    execute_claimed_image,
                    image_request_fingerprint,
                    is_effect_replayable,
                    set_staged_path,
                )

                effect_extra = {
                    "mode": raw_mode,
                    "source_artifact_id": raw_source_artifact_id,
                    "canvas_width": raw_canvas_width,
                    "canvas_height": raw_canvas_height,
                    "session_id": raw_session_id,
                    "revision": raw_revision,
                    "work_sha256": raw_work_sha256,
                    "mask_sha256": raw_mask_sha256,
                    "reference_sha256": raw_reference_sha256,
                    "strength": raw_strength,
                    "mask_kind": raw_mask_kind,
                    "source_transform": raw_source_transform,
                    "source_size": raw_source_size,
                }
                effect_fingerprint = image_request_fingerprint(
                    str(raw_prompt or "").strip(),
                    str(raw_aspect_ratio or "1:1").strip(),
                    raw_references,
                    raw_seed,
                    extra=effect_extra,
                )
                try:
                    prompt, fmt, refs, seed = _svc.validate_request(
                        str(raw_prompt or "").strip(),
                        str(raw_aspect_ratio or "1:1").strip(),
                        raw_references,
                        raw_seed,
                        mask_kind=raw_mask_kind,
                        mode=raw_mode,
                    )
                except ValueError as exc:
                    fail(exc, "bad_request")
                    return
                try:
                    available, reason = _svc.is_available()
                except Exception as exc:
                    fail(exc, "backend_error")
                    return
                if not available:
                    fail(RuntimeError(reason))
                    return
                effect_db = _ImageSession()
                try:
                    effect_claim = claim_image_effect(
                        effect_db,
                        run_id=run_id,
                        logical_action_id=f"image:{effect_fingerprint[:32]}",
                        output_index=0,
                        fingerprint=effect_fingerprint,
                    )
                except (EffectConflict, EffectInProgress) as exc:
                    effect_db.close()
                    fail(exc)
                    return
                except Exception as exc:
                    effect_db.close()
                    fail(exc, "STORAGE_ERROR")
                    return
                if (
                    effect_claim.status == "published"
                    and effect_claim.output_artifact_id
                    and is_effect_replayable(effect_db, effect_claim.effect_id)
                ):
                    persist_state("completed", effect_claim.output_artifact_id)
                    _emit_artifact_event(
                        effect_db,
                        request_id=request_id,
                        run_id=run_id,
                        artifact_id=effect_claim.output_artifact_id,
                        deduplicated=True,
                    )
                    effect_db.close()
                    emit(
                        "run_completed",
                        {
                            "request_id": request_id,
                            "run_id": run_id,
                            "status": "completed",
                            "artifact_id": effect_claim.output_artifact_id,
                            "deduplicated": True,
                        },
                        run_id=run_id,
                        conversation_id=None,
                    )
                    return
                if effect_claim.status != "claimed":
                    effect_db.close()
                    fail(RuntimeError(f"[EFFECT_{effect_claim.status.upper()}]"))
                    return
                emit(
                    "run_stage",
                    {
                        "request_id": request_id,
                        "run_id": run_id,
                        "stage": "production",
                        "status": "started",
                    },
                    run_id=run_id,
                    conversation_id=None,
                )

                def stage(info: dict):
                    try:
                        emit(
                            "run_stage",
                            {
                                "request_id": request_id,
                                "run_id": run_id,
                                "stage": info.get("id", "production"),
                                "status": info.get("label", "")[:200],
                                "step": info.get("step"),
                                "total": info.get("total"),
                            },
                            run_id=run_id,
                            conversation_id=None,
                        )
                    except Exception:
                        pass

                def generate_effect(effective_seed, effect_id, before_commit):
                    from backend.config.paths import get_data_dir

                    staged_path = str(
                        Path(get_data_dir()) / "staged" / f"{effect_id}.png"
                    )
                    set_staged_path(effect_db, effect_id, staged_path)
                    from backend.ipc.images import (
                        image_generate_options as _image_generate_options,
                    )

                    _image_opts = _image_generate_options(params)
                    return _svc.generate(
                        manager,
                        run_id,
                        prompt,
                        fmt,
                        refs,
                        effective_seed,
                        conv_id=None,
                        idempotency_key=effect_id,
                        cancel_flag=cancel_flag,
                        on_stage=stage,
                        origin="studio",
                        before_commit=before_commit,
                        staged_path=staged_path,
                        mode=raw_mode,
                        work_token=raw_work_token,
                        mask_token=raw_mask_token,
                        reference_tokens=raw_reference_tokens,
                        work_sha256=raw_work_sha256,
                        mask_sha256=raw_mask_sha256,
                        reference_sha256=raw_reference_sha256,
                        source_artifact_id=raw_source_artifact_id,
                        canvas_width=raw_canvas_width,
                        canvas_height=raw_canvas_height,
                        strength=raw_strength,
                        session_id=raw_session_id,
                        revision=raw_revision,
                        mask_kind=raw_mask_kind,
                        source_transform=_image_opts.get(
                            "source_transform", raw_source_transform
                        ),
                        source_size=_image_opts.get("source_size", raw_source_size),
                    )

                try:
                    _claim, pub = execute_claimed_image(
                        effect_db,
                        effect_claim,
                        generate_effect,
                        on_publish=mark_published,
                    )
                except (EffectConflict, EffectInProgress) as exc:
                    effect_db.close()
                    fail(exc)
                    return
                except Exception as exc:
                    effect_db.close()
                    fail(exc)
                    return
                if not isinstance(pub, dict) or not pub.get("artifact_id"):
                    effect_db.close()
                    fail(
                        RuntimeError(
                            "[PUBLISH_FAILED] Publication image sans artifact_id."
                        )
                    )
                    return
                artifact_id = str(pub["artifact_id"])
                stage(
                    {"id": "transfert", "label": "Transfert de l’image vers l’atelier"}
                )
                _emit_artifact_event(
                    effect_db,
                    request_id=request_id,
                    run_id=run_id,
                    artifact_id=artifact_id,
                )
                effect_db.close()
                emit(
                    "run_completed",
                    {
                        "request_id": request_id,
                        "run_id": run_id,
                        "status": "completed",
                        "artifact_id": artifact_id,
                    },
                    run_id=run_id,
                    conversation_id=None,
                )
            finally:
                clear_cancel_flag(run_id)
            return

        finally:
            liberer(reservation)

    elif method == "web.settings":
        # Les deux réglages de la recherche web. Réponse et écriture dans la
        # MÊME méthode : relire après avoir écrit est la seule façon de ne pas
        # annoncer un réglage appliqué qui ne l'est pas.
        from backend.inference import web_settings as _ws

        emit(
            "web_settings",
            {"request_id": request_id, **_ws.apply_and_describe(params)},
            run_id=run_id,
        )
        return

    elif method == "image.status":
        # Diagnostic affiché par la page Images : ce que le sidecar voit vraiment
        # (env, fichiers, CUDA) — pour trancher "je ne peux pas" sans deviner.
        from backend.services import image_service as _svc0
        from backend.services import flux_spike as _fx0

        try:
            ok, reason = _svc0.is_available()
            _, details = _fx0.probe()
        except Exception as exc:
            ok, reason, details = False, _safe_public_text(exc)[:300], {}
        reason = _safe_public_text(reason)
        details = {
            str(key): _safe_public_text(value) if isinstance(value, str) else value
            for key, value in (details or {}).items()
        }
        details.pop("flux_dir", None)
        emit(
            "image_status",
            {
                "request_id": request_id,
                "available": ok,
                "reason": reason,
                "enabled": _fx0.backend_enabled(),
                "details": details,
            },
            run_id=run_id,
        )
        return

    elif method in _IPC_HANDLERS:
        # Commandes portees depuis le pont Electron (qui les faisait en rusqlite) :
        # le sidecar Python possede desormais la base, donc il les sert lui-meme.
        _dispatch_ipc(method, params, request_id, run_id)

    elif method in ("health", "resync"):
        emit(
            "error",
            {
                "request_id": request_id,
                "code": "not_implemented_via_sidecar",
                "message": f"{method} n'est pas implemente",
            },
            run_id=request_id,
        )

    else:
        emit(
            "error",
            {
                "request_id": request_id,
                "code": "unknown_method",
                "message": f"Unknown {method}",
                "retryable": False,
            },
            run_id=request_id,
        )


MAX_ARTEFACTS_GC_DEMARRAGE = 500
DELAI_GC_DEMARRAGE_S = 0.5


def _gc_et_entretien_demarrage() -> None:
    """Corbeille, GC et nettoyages de démarrage, hors de la boucle stdin.

    `backend_ready` est déjà émis : l'application répond pendant que cette
    passe travaille. Chaque tâche a son propre `try/except` — un échec de
    corbeille ne doit pas sauter le GC, et inversement. Le GC est borné
    (`MAX_ARTEFACTS_GC_DEMARRAGE`) et protégé par `verrou_gc`, partagé avec le
    CLI `--gc`.

    SCAN DE `data/blobs` : cette passe fait DEUX parcours, et seulement deux,
    dans le même fil de fond. `purger_orphelins_blobs_expires` cherche les
    `.bin` expirés ; `_signaler_depot_degrade` refait un inventaire complet pour
    diagnostiquer les anomalies du dépôt (liens, fiches, index). Les deux
    fonctions vivent dans `depot_integrite.py` et ne partagent pas encore
    d'inventaire ; les fusionner demanderait de changer leur API, hors du
    périmètre Phase 7. Aucun AUTRE chemin de démarrage ne parcourt `blobs` :
    les deux scans sont consécutifs, bornés par le TTL et exécutés après
    `backend_ready`, donc ils ne bloquent jamais `stdin_loop`.
    """
    from backend.db.database import SessionLocal, _signaler_depot_degrade
    from backend.ipc.attachments import (
        purger_staging_expires,
        purger_temporaires_blobs_expires,
        purger_temporaires_temp_expires,
    )
    from backend.search import trash as _trash
    from backend.services import corbeille as _corbeille
    from backend.services import depot_integrite as _di

    time.sleep(DELAI_GC_DEMARRAGE_S)

    def _rollback(db) -> None:
        try:
            db.rollback()
        except Exception as exc:  # noqa: BLE001 - le démarrage doit continuer
            log(f"[gc] rollback impossible: {_safe_public_text(exc)}")

    try:
        with _di.verrou_gc():
            db = SessionLocal()
            try:
                try:
                    purgees = _corbeille.balayer_corbeille(db)
                    db.commit()
                    if purgees:
                        log(
                            f"[corbeille] {purgees} conversation(s) expirée(s) purgée(s)"
                        )
                except Exception as exc:  # noqa: BLE001
                    _rollback(db)
                    log(f"[corbeille] balayage impossible: {_safe_public_text(exc)}")

                try:
                    gc = _trash.purge(db, limite=MAX_ARTEFACTS_GC_DEMARRAGE)
                    db.commit()
                    if gc.get("links") or gc.get("artifacts") or gc.get("blobs"):
                        log(
                            f"[gc] {gc.get('links', 0)} lien(s), "
                            f"{gc.get('artifacts', 0)} artefact(s), "
                            f"{gc.get('blobs', 0)} blob(s) purgé(s)"
                        )
                    if gc.get("chunks") or gc.get("index_chunks") or gc.get("vecteurs"):
                        log(
                            f"[gc] {gc.get('chunks', 0)} chunk(s), "
                            f"{gc.get('index_chunks', 0)} ligne(s) chunk_search, "
                            f"{gc.get('vecteurs', 0)} vecteur(s) purgé(s)"
                        )
                    if gc.get("links_repares"):
                        log(
                            f"[gc] {gc.get('links_repares')} lien(s) "
                            "d'artefact supprimé réparé(s)"
                        )
                    if gc.get("artifacts_failed") or gc.get("errors"):
                        log(
                            f"[gc] {gc.get('artifacts_failed', 0)} artefact(s) en "
                            f"échec ; {len(gc.get('errors') or [])} erreur(s) nommée(s)"
                        )
                        for erreur in (gc.get("errors") or [])[:5]:
                            log(
                                f"[gc] échec {erreur.get('artifact_id')}: "
                                f"{erreur.get('error')}"
                            )
                except Exception as exc:  # noqa: BLE001
                    _rollback(db)
                    log(f"[gc] purge impossible: {_safe_public_text(exc)}")

                try:
                    orphelins = _di.purger_orphelins_blobs_expires(db)
                    if orphelins.get("purged") or orphelins.get("errors"):
                        log(
                            f"[blobs] {orphelins.get('purged', 0)} .bin orphelin(s) "
                            f"expiré(s) purgé(s) ; "
                            f"{len(orphelins.get('errors') or [])} erreur(s)"
                        )
                except Exception as exc:  # noqa: BLE001
                    log(f"[blobs] récupération impossible: {_safe_public_text(exc)}")
            finally:
                try:
                    db.close()
                except Exception:
                    pass
    except Exception as exc:  # noqa: BLE001 - verrou indisponible : démarrage continue
        log(f"[gc] passerelle indisponible: {_safe_public_text(exc)}")

    # Nettoyages sans base : indépendants du verrou et du GC.
    try:
        _stagings_purges = purger_staging_expires()
        if _stagings_purges:
            log(f"[staging] {_stagings_purges} fichier(s) expiré(s) purgé(s)")
        _tmp_purges = (
            purger_temporaires_temp_expires() + purger_temporaires_blobs_expires()
        )
        if _tmp_purges:
            log(f"[temp] {_tmp_purges} temporaire(s) expiré(s) purgé(s)")
    except Exception as exc:  # noqa: BLE001
        log(f"[staging] purge impossible: {_safe_public_text(exc)}")

    # Scan complet du dépôt (Phase 7.3) : différé APRÈS `backend_ready` pour ne
    # pas bloquer la boucle stdin sur un gros dépôt.
    try:
        _signaler_depot_degrade()
    except Exception as exc:  # noqa: BLE001
        log(f"[depot] contrôle différé impossible: {_safe_public_text(exc)}")


def demarrer_gc_demarrage() -> "threading.Thread":
    """Lance la passe de fond et rend la main IMMÉDIATEMENT.

    Le thread est daemon : un arrêt du sidecar ne l'attend pas. Le test
    d'asynchronisme vérifie que cette fonction ne bloque pas.
    """
    fil = threading.Thread(
        target=_gc_et_entretien_demarrage,
        name="gc-demarrage",
        daemon=True,
    )
    fil.start()
    return fil


def demarrer_reprise_demarrage() -> "threading.Thread":
    """Rejoue l'outbox et le balayage de reprise HORS du chemin de démarrage.

    `_replay_pending_artifact_events` et `demarrer_reprise` lisent la base et
    peuvent lancer des extractions. Les exécuter avant `stdin_loop` faisait
    attendre le premier `backend_ready` derrière un scan de base long — le
    défaut que ce fil supprime. Les deux étapes restent dans le même fil
    daemon, dans l'ordre historique (rejeu puis reprise) ; un échec de l'une
    n'empêche pas l'autre, et l'arrêt du sidecar ne l'attend pas.
    """

    def _cible() -> None:
        try:
            _replay_pending_artifact_events()
        except Exception as exc:  # noqa: BLE001 — le démarrage ne casse pas
            log(f"[outbox] replay différé impossible: {_safe_public_text(exc)}")
        try:
            from backend.ingestion.reprise import demarrer_reprise as _reprise

            _reprise()
        except Exception as exc:  # noqa: BLE001 — le démarrage ne casse pas
            log(f"[reprise] balayage différé impossible: {_safe_public_text(exc)}")

    fil = threading.Thread(
        target=_cible,
        name="reprise-demarrage",
        daemon=True,
    )
    fil.start()
    return fil


def acquerir_verrou_gc_sidecar() -> bool:
    """Garde le verrou GC pendant la vie du sidecar, sans bloquer le démarrage.

    Le verrou est une protection de CONCURRENCE inter-processus, pas une
    condition d'existence : une plateforme qui ne peut pas le fournir (Windows
    sans le fallback `msvcrt`, ou un verrou déjà tenu) ne doit pas empêcher le
    sidecar de répondre. Le CLI `--gc` garde le comportement inverse : il
    refuse de tourner sans verrou, parce que lui ne fait QUE supprimer.
    """
    from backend.services import depot_integrite as _di_verrou

    try:
        _di_verrou.acquerir_verrou_gc_vie()
        return True
    except RuntimeError as exc:
        # Fail-open : la passe de fond réessaiera son propre `verrou_gc()` et
        # journalisera l'absence de protection. Refuser le démarrage ici
        # rendrait le produit inutilisable sur une cible sans `flock`.
        log(
            "[gc] verrou de vie indisponible, démarrage sans protection "
            f"inter-processus : {_safe_public_text(exc)}"
        )
        return False


if __name__ == "__main__":
    acquerir_verrou_gc_sidecar()
    init_db()
    # Phase 8.4 — extraction documentaire structurée. Le hook du service reçoit
    # le runtime chat (manager + coordinateur GPU) ; aucun modèle n'est chargé
    # ici, et un test qui importe `backend.main` n'installe rien.
    from backend.services.document_extraction_llm import installer_extracteur_llm

    installer_extracteur_llm(get_chat_runtime)
    # Le planificateur mémoire démarre après la base : il ne fait que lire des
    # seuils et n'appelle aucun modèle. `shutdown_active_runs` l'arrête aussi.
    from backend.services.memoire_scheduler import (
        demarrer_planificateur_documentaire,
        demarrer_planificateur_memoire,
    )

    demarrer_planificateur_memoire()
    # Backfill/ledger documentaire : boucle dédiée de 5 min, indépendante des
    # cinq tours et de la reprise d'ingestion.
    demarrer_planificateur_documentaire()
    # Étape B — description différée des figures. Le manager est construit (et
    # sa couche vision enregistrée) : le worker peut démarrer. Son échec ne
    # bloque jamais `backend_ready` : c'est un fond daemon, et les figures
    # `pending` restent réclamables au prochain démarrage.
    try:
        from backend.ingestion.figures_worker import demarrer_worker_figures

        demarrer_worker_figures()
    except Exception as exc:
        log(f"[figures] worker non démarré: {_safe_public_text(exc)}")
    log(
        f"[thot-phase-1] backend_ready backend={manager.backend.backend} vram={manager.backend.free_vram // (1024 * 1024)}MB proto={PROTOCOL_VERSION}"
    )
    # Émet backend_ready versionné + legacy
    emit(
        "backend_ready",
        {
            "backend": manager.backend.backend,
            "vram": manager.backend.free_vram,
            "n_gpu_layers": manager.backend.n_gpu_layers,
            "protocol_version": PROTOCOL_VERSION,
        },
    )
    emit(
        "backend_ready",
        {
            "backend": manager.backend.backend,
            "vram": manager.backend.free_vram,
            "protocol_version": PROTOCOL_VERSION,
        },
        run_id=None,
    )  # C08 canonique
    # Rejeu de l'outbox et balayage de reprise : dans un thread daemon APRÈS
    # `backend_ready`. Le premier `stdin_loop` ne doit pas attendre un scan DB
    # long ; l'ordre historique (rejeu puis reprise) est conservé dans le fil.
    demarrer_reprise_demarrage()
    # Corbeille, GC et nettoyages : dans un thread daemon APRÈS `backend_ready`.
    # Le démarrage ne doit pas attendre un rglob complet du dépôt ni un commit
    # par artefact avant d'écouter stdin. Les étapes sont indépendantes et
    # bornées ; le détail est dans `_gc_et_entretien_demarrage`.
    demarrer_gc_demarrage()
    # Reprise PÉRIODIQUE (Phase 2, tâche 2.4) : démarre ICI, après le balayage
    # de démarrage et avant la boucle stdin. Le timer n'appelle `balayer_reprise`
    # qu'une fois le manager prêt, avec `modeles_autorises=True`, et se tait si
    # `tour_actif()` : il ne prend jamais de bail GPU lui-même.
    from backend.ingestion.reprise import demarrer_reprise_periodique

    demarrer_reprise_periodique(est_pret=lambda: manager.est_pret)
    try:
        stdin_loop(handle, on_shutdown=shutdown_active_runs)
    finally:
        from backend.ingestion.reprise import arreter_reprise_periodique

        arreter_reprise_periodique()
        from backend.services.memoire_scheduler import (
            arreter_planificateur_documentaire,
            arreter_planificateur_memoire,
        )

        arreter_planificateur_documentaire()
        arreter_planificateur_memoire()
        from backend.ingestion.figures_worker import arreter_worker_figures

        arreter_worker_figures()
