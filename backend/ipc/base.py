"""Handlers IPC des conversations.

Portage des commandes qui vivaient en `rusqlite` dans le pont Electron
(`electron/main.cjs:590-850`) vers Python. Le portage preserve deux
comportements volontairement « laxistes » du pont d'origine, parce que le
frontend en dépend pour ne pas casser l'écran :

  - une base injoignable rend une liste vide, pas une erreur ;
  - un id inconnu rend `None` / 404 logique, jamais une exception.

Les colonnes de retour restent en `snake_case` : le frontend TypeScript
(`frontend/src/lib/ipcBridge.ts:102-155`) les lit ainsi.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.ipc.images import _iso_text, _param, _root_message

PREVIEW_MAX_CHARS = 200
LIST_LIMIT_DEFAULT = 50
LIST_LIMIT_MAX = 100
ATTACHMENT_UNAVAILABLE = {"id": "", "kind": "image", "name": "pièce jointe", "mime": ""}


def _limit(params: dict[str, Any]) -> int:
    raw = _param(params, "limit")
    try:
        value = int(raw) if raw is not None else LIST_LIMIT_DEFAULT
    except (TypeError, ValueError):
        value = LIST_LIMIT_DEFAULT
    return max(1, min(LIST_LIMIT_MAX, value))


def _offset(params: dict[str, Any]) -> int:
    raw = _param(params, "offset")
    try:
        return max(0, int(raw)) if raw is not None else 0
    except (TypeError, ValueError):
        return 0


def _conversation_id(params: dict[str, Any]) -> str:
    value = _param(params, "conversationId", "conversation_id", "id")
    if not isinstance(value, str) or not value:
        raise ValueError("Conversation introuvable.")
    return value


def list_conversations(db: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Liste paginée des conversations, la plus récemment active d'abord.

    Chaque entrée porte un aperçu du dernier message et le nombre de messages,
    tous deux recalculés : le pont Electron les fait aussi en requêtes
    best-effort, et ne les exposait pas comme colonnes stockées.

    Deux choix qui ne relèvent pas du détail :


    - `workspace_id` est retourné pour que l'interface puisse regrouper par
      espace. Sans lui, tout chat est indistinct : deux conversations portant le
      même titre, l'une dans un espace et l'autre en dehors, seraient
      indiscernables.
    - Les conversations en corbeille (`deleted_at` non nul) ne sont pas
      listées : elles sont restaurables sept jours, pas visibles — §4.1 de
      `cycle_de_vie_des_informations.md` les retire des vues actives.
    """
    limit, offset = _limit(params), _offset(params)
    rows = (
        db.execute(
            text(
                """
            SELECT c.id, c.title, c.created_at, c.updated_at,
                   w.id AS workspace_id, w.name AS workspace_name,
                   (SELECT m.content FROM messages m
                     WHERE m.conv_id = c.id AND m.role = 'user'
                     ORDER BY m.created_at DESC LIMIT 1) AS last_user,
                   (SELECT m.content FROM messages m
                     WHERE m.conv_id = c.id
                     ORDER BY m.created_at DESC LIMIT 1) AS last_any,
                   (SELECT COUNT(*) FROM messages m WHERE m.conv_id = c.id) AS message_count
              FROM conversations c
              LEFT JOIN workspaces w ON w.id = c.workspace_id
             WHERE c.deleted_at IS NULL
             ORDER BY COALESCE(c.updated_at, c.created_at) DESC, c.id ASC
             LIMIT :limit OFFSET :offset
            """
            ),
            {"limit": limit, "offset": offset},
        )
        .mappings()
        .all()
    )

    items: list[dict[str, Any]] = []
    for row in rows:
        preview = row["last_user"] or row["last_any"] or ""
        items.append(
            {
                "id": row["id"],
                "title": row["title"] or "Sans titre",
                "workspace_id": row["workspace_id"],
                "workspace_name": row["workspace_name"],
                "created_at": _iso_text(row["created_at"]),
                "updated_at": _iso_text(row["updated_at"]),
                "last_message_preview": (preview or "")[:PREVIEW_MAX_CHARS],
                "message_count": int(row["message_count"] or 0),
            }
        )
    return {"conversations": items, "count": len(items)}


def get_conversation(db: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Une conversation, ou `None` si elle n'existe pas.

    Une conversation en corbeille est une conversation absente des vues
    actives (cycle de vie §4.1) : le payload vide, pas un objet marqué.
    """
    row = (
        db.execute(
            text(
                "SELECT id, title, mode, created_at, updated_at FROM conversations"
                " WHERE id = :id AND deleted_at IS NULL"
            ),
            {"id": _conversation_id(params)},
        )
        .mappings()
        .first()
    )
    if row is None:
        return {}
    return {
        "id": row["id"],
        "title": row["title"] or "Sans titre",
        "mode": row["mode"] or "chat",
        "created_at": _iso_text(row["created_at"]),
        "updated_at": _iso_text(row["updated_at"]),
    }


def list_messages(db: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Historique d'une conversation, du plus ancien au plus récent.

    Trois familles sont réunies ici parce que le schéma les a faire évoluer :
    `messages.attachments` (normatif), `runs.output_artifact_id` (artefacts
    produits par le modèle) et la table `sources` (héritage v0.8). Elles sont
    dédupliquées par id d'artefact, la source normative primant.

    Une conversation en corbeille (ou inconnue) rend un historique vide :
    §4.1 retire ses messages des vues actives, et le pont d'origine rendait
    déjà une liste vide pour un id inconnu — même 404 logique, jamais
    d'exception.
    """
    conversation_id = _conversation_id(params)
    existe = db.execute(
        text("SELECT 1 FROM conversations WHERE id = :id AND deleted_at IS NULL"),
        {"id": conversation_id},
    ).first()
    if existe is None:
        return {"messages": [], "count": 0}
    rows = (
        db.execute(
            text(
                "SELECT id, conv_id, role, content, thinking, orchestrator_thinking,"
                " brief, brief_json, plan_json, created_at"
                " FROM messages WHERE conv_id = :id ORDER BY created_at ASC, id ASC"
            ),
            {"id": conversation_id},
        )
        .mappings()
        .all()
    )

    messages = [dict(row) for row in rows]
    for message in messages:
        message["brief"] = message.pop("brief", None)
        message["brief_json"] = _json_or_none(message.pop("brief_json", None))
        message["plan"] = _json_or_none(message.pop("plan_json", None))
        message["created_at"] = _iso_text(message["created_at"])
        message["attachments"] = _attachments_for(db, message["id"])
        message["toolcalls"] = _toolcalls_for(db, message["id"])
    return {"messages": messages, "count": len(messages)}


def rename_conversation(db: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Renomme une conversation. Un titre vide ou blanc est refusé."""
    conversation_id = _conversation_id(params)
    title = _param(params, "title")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("Le titre ne peut pas être vide.")
    _require_conversation(db, conversation_id)
    db.execute(
        text(
            "UPDATE conversations SET title = :title, updated_at = CURRENT_TIMESTAMP WHERE id = :id"
        ),
        {"title": title.strip(), "id": conversation_id},
    )
    db.commit()
    return {"ok": True, "id": conversation_id, "title": title.strip()}


def delete_conversation(db: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Mets une conversation à la corbeille. Rien n'est détruit immédiatement.

    §4.1 de `cycle_de_vie_des_informations.md` : la suppression retire la
    discussion des vues et usages actifs, pas de l'existence. Le verbe public
    reste `delete.conversation` ; le changement est interne — la corbeille dure
    sept jours (`services/corbeille.py`, annexe10:146) et le nettoyage ciblé
    (liens, `doc_refs`, index de portée conversation, web conservé) s'exécute
    dès la mise en corbeille. L'ancienne suppression physique en cascade
    emportait la conversation ET l'accès à ses images préservées, ce que la
    fiche refuse.
    """
    conversation_id = _conversation_id(params)
    from backend.services import corbeille as service

    return service.mettre_en_corbeille(db, conversation_id)


def _require_conversation(db: Session, conversation_id: str) -> None:
    """Lève avant toute écriture : un renommage ou une suppression sur un id
    inconnu doit échouer, pas créer un silence.

    Une conversation EN CORBEILLE est introuvable pour les usages actifs :
    §4.1 la retire des vues, et la corbeille (état, pas portée) ne redevient
    lisible qu'après restauration — hors périmètre de ce lot.
    """
    found = db.execute(
        text("SELECT 1 FROM conversations WHERE id = :id AND deleted_at IS NULL"),
        {"id": conversation_id},
    ).first()
    if found is None:
        raise ValueError("Conversation introuvable.")


# ── Pièces jointes et appels d'outils ────────────────────────────────────────


def _attachments_for(db: Session, message_id: str) -> list[dict[str, Any]]:
    """Pièces jointes d'un message, dédupliquées par id d'artefact.

    Trois origines, dédupliquées dans cet ordre de priorité — le pont Electron
    (`electron/main.cjs:741-805`) faisait exactement cette union :

      1. `message_attachments` — la source normative ;
      2. `runs.output_artifact_id` — les images **produites par le modèle**, qui
         n'ont pas de ligne dans `message_attachments` ; sans cette source
         l'historique d'une génération d'image s'affiche sans aucune image ;
      3. `sources` — l'héritage v0.8.
    """
    merged: dict[str, dict[str, Any]] = {}

    for row in (
        db.execute(
            text(
                """
            SELECT a.id AS artifact_id, a.kind, a.original_filename, a.display_name,
                   b.mime_detected
              FROM message_attachments ma
              JOIN artifacts a ON a.id = ma.artifact_id AND a.deleted_at IS NULL
         LEFT JOIN blobs b ON b.id = a.blob_id
             WHERE ma.message_id = :id
             ORDER BY ma.ordinal ASC
            """
            ),
            {"id": message_id},
        )
        .mappings()
        .all()
    ):
        attachment = _attachment(row)
        merged.setdefault(attachment["id"], attachment)

    for row in (
        db.execute(
            text(
                """
            SELECT a.id AS artifact_id, a.kind, a.original_filename, a.display_name,
                   b.mime_detected
              FROM messages m
              JOIN runs r ON r.id = m.run_id
              JOIN artifacts a ON a.id = r.output_artifact_id AND a.deleted_at IS NULL
         LEFT JOIN blobs b ON b.id = a.blob_id
             WHERE m.id = :id AND m.role = 'assistant'
            """
                # `role = 'assistant'` : le user ET l'assistant partagent le meme
                # run_id, donc sans ce filtre l'image generee s'affiche deux fois.
            ),
            {"id": message_id},
        )
        .mappings()
        .all()
    ):
        attachment = _attachment(row)
        merged.setdefault(attachment["id"], attachment)

    # Héritage v0.8 : ces entrées n'ont pas d'artefact, seulement un fichier
    # indexé par empreinte. On les expose quand même, sinon des discussions
    # anciennes perdent leurs pièces.
    try:
        for row in (
            db.execute(
                text(
                    "SELECT id, source_type, original_name, mime_type FROM sources"
                    " WHERE msg_id = :id"
                ),
                {"id": message_id},
            )
            .mappings()
            .all()
        ):
            key = f"src_{row['id']}"
            if key in merged:
                continue
            merged[key] = {
                "id": key,
                "kind": row["source_type"] or "file",
                "name": row["original_name"] or "fichier",
                "mime": row["mime_type"] or "",
            }
    except Exception:  # noqa: BLE001 - table absente sur une base neuve
        pass

    return list(merged.values())


def _attachment(row: Any) -> dict[str, Any]:
    return {
        "id": row["artifact_id"] or "",
        "kind": row["kind"] or "image",
        "name": row["original_filename"] or row["display_name"] or "pièce jointe",
        "mime": row["mime_detected"] or "",
    }


def _toolcalls_for(db: Session, message_id: str) -> list[dict[str, Any]]:
    rows = (
        db.execute(
            text(
                "SELECT tool_call_id, tool, args, result, ok, latency_ms"
                " FROM toolcalls WHERE msg_id = :id ORDER BY ordinal ASC, id ASC"
            ),
            {"id": message_id},
        )
        .mappings()
        .all()
    )
    calls: list[dict[str, Any]] = []
    for row in rows:
        calls.append(
            {
                "tool_call_id": row["tool_call_id"] or "",
                "tool": row["tool"] or "",
                "arguments": _json_or_none(row["args"]) or {},
                "result": row["result"] or "",
                "ok": bool(row["ok"]) if row["ok"] is not None else False,
                "latency_ms": int(row["latency_ms"] or 0),
            }
        )
    return calls


def _json_or_none(raw: Any) -> Any:
    """Un JSON stocké en texte peut être absent, `null` ou corrompu : jamais lever."""
    import json

    if raw is None or raw == "":
        return None
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


__all__ = [
    "list_conversations",
    "get_conversation",
    "list_messages",
    "rename_conversation",
    "delete_conversation",
]
