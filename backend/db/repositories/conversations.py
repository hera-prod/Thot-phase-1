# db/repositories/conversations.py — Phase 1 simplifié (fusion v0.8)
# Notes FR : CRUD conversations + messages, double historique partagé sans tool traces passées
# Supprimé v0.8 : space_id, model_used, documents, FTS, memory — garde 4 tables seulement
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, selectinload

from backend.db.models import Conversation, Message


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid.uuid4())


# --- Conversations ---

def create_conversation(
    db: Session,
    *,
    title: str | None = None,
    conv_id: str | None = None,
    workspace_id: str | None = None,
) -> Conversation:
    """Crée une conversation (Phase 1 : pas de model_used).

    `mode` n'est plus un paramètre : il n'existe qu'un mode de discussion. La
    colonne reste écrite à "chat" parce qu'elle est NOT NULL dans les données
    historiques, mais plus aucun appelant ne choisit sa valeur.

    `workspace_id` écrit l'espace d'appartenance à la création. Son absence
    laisse la colonne à son défaut (chat simple) : l'adhésion est facultative,
    donc ne pas la nommer est un choix, pas un oubli.
    """
    if not title:
        title = "Nouvelle conversation"
    conv = Conversation(
        id=conv_id or new_id(),
        title=title.strip() or "Nouvelle conversation",
        mode="chat",
    )
    if workspace_id:
        conv.workspace_id = workspace_id
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv


def get_conversation(db: Session, conversation_id: str) -> Conversation | None:
    """Récupère une conversation avec messages + toolcalls (eager)."""
    if not conversation_id:
        return None
    stmt = (
        select(Conversation)
        .where(Conversation.id == conversation_id)
        .options(
            selectinload(Conversation.messages).selectinload(Message.toolcalls),
            selectinload(Conversation.messages).selectinload(Message.sources),
        )
    )
    return db.execute(stmt).scalar_one_or_none()


def list_conversations(
    db: Session,
    *,
    limit: int = 50,
    offset: int = 0,
) -> Sequence[Conversation]:
    """Révolu : voir backend/ipc/base.py::list_conversations.

    L'exposé n'est plus en `__all__` : le listing réel filtre la corbeille
    (`deleted_at IS NULL`), cette version legacy ne le fait pas et resterait
    un risque de court-circuit.
    """
    stmt = (
        select(Conversation)
        .order_by(Conversation.updated_at.desc())
        .offset(offset)
        .limit(limit)
    )
    return db.execute(stmt).scalars().all()


def rename_conversation(db: Session, *, conversation_id: str, title: str) -> Conversation | None:
    """Révolu : voir backend/ipc/base.py::rename_conversation.

    L'exposé n'est plus en `__all__` : le renommage réel refuse d'écrire dans
    une conversation en corbeille (`_require_conversation`,
    `deleted_at IS NULL`), cette version legacy l'accepterait.
    """
    conv = db.get(Conversation, conversation_id)
    if conv is None:
        return None
    conv.title = title.strip() or conv.title
    db.commit()
    db.refresh(conv)
    return conv


def delete_conversation(db: Session, *, conversation_id: str) -> bool:
    """Mise à la corbeille (sept jours), pas un DELETE (cycle de vie §4.1).

    Le dépôt tenait ce chemin pour un DELETE en cascade : c'était le seul
    ancien accès à la suppression physique. Tout appelant passe désormais par
    le service `backend.services.corbeille` — le dépôt ne détient plus de
    décision de cycle de vie.
    """
    from backend.services import corbeille as service

    conv = db.get(Conversation, conversation_id)
    if conv is None:
        return False
    service.mettre_en_corbeille(db, conversation_id)
    return True


# --- Messages (historique partagé user↔assistant) ---

def append_message(
    db: Session,
    *,
    conversation_id: str,
    role: str,
    content: str,
    thinking: str | None = None,
    run_id: str | None = None,
    status: str = "completed",
    tokens_in: int = 0,
    tokens_out: int = 0,
    # Trace orchestrateur (bulle UI) — posée par main.py depuis le retour loop, jamais par défaut
    orchestrator_thinking: str | None = None,
    brief: str | None = None,
    plan_json: str | None = None,
) -> Message:
    """Ajoute un message. Guard : role valide, conversation existe et active."""
    if role not in ("user", "assistant", "system"):
        raise ValueError(f"role invalide : {role}")
    if not content and role != "system":
        raise ValueError("content vide")
    # Une conversation en corbeille n'accepte rien : écrire dedans ressusciterait
    # une discussion que §4.1 vient de retirer des usages actifs. Le garde
    # bloque AVANT l'écriture (échouer vite vaut mieux qu'aggraver).
    en_corbeille = db.execute(
        select(Conversation.deleted_at).where(Conversation.id == conversation_id)
    ).scalar_one_or_none()
    if en_corbeille is not None:
        raise ValueError("Conversation introuvable.")
    msg = Message(
        id=new_id(),
        conv_id=conversation_id,
        role=role,
        content=content,
        thinking=thinking,
        run_id=run_id,
        status=status,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        orchestrator_thinking=orchestrator_thinking,
        brief=brief,
        plan_json=plan_json,
    )
    db.add(msg)
    conv = db.get(Conversation, conversation_id)
    if conv is not None:
        conv.updated_at = utcnow()
    db.commit()
    db.refresh(msg)
    return msg


def list_messages(
    db: Session,
    *,
    conversation_id: str,
    limit: int | None = None,
    offset: int = 0,
) -> Sequence[Message]:
    stmt = (
        select(Message)
        .where(Message.conv_id == conversation_id)
        .options(selectinload(Message.toolcalls), selectinload(Message.sources))
        .order_by(Message.created_at.asc())
        .offset(offset)
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return db.execute(stmt).scalars().all()


def count_messages(db: Session, *, conversation_id: str) -> int:
    stmt = select(func.count(Message.id)).where(Message.conv_id == conversation_id)
    return int(db.execute(stmt).scalar_one())


def purge_messages_for_conversation(db: Session, *, conversation_id: str) -> int:
    stmt = delete(Message).where(Message.conv_id == conversation_id)
    result = db.execute(stmt)
    db.commit()
    return int(getattr(result, "rowcount", 0) or 0)  # CursorResult.rowcount (Result générique ne l'expose pas)


def load_conversation_history(
    db: Session,
    conversation_id: str,
    *,
    limit: int | None = None,
) -> list[Message]:
    """Historique partagé pour LLM : user↔assistant, trié ancien→récent."""
    if not conversation_id:
        return []
    stmt = (
        select(Message)
        .where(Message.conv_id == conversation_id)
        .order_by(Message.created_at.asc(), Message.id.asc())
    )
    if limit is not None and limit > 0:
        stmt = stmt.limit(limit)
    return list(db.scalars(stmt).all())


def get_orchestrator_context(
    db: Session,
    conversation_id: str,
    max_tokens: int = 128_000,
) -> list[dict]:
    """
    Construit historique partagé pour LLM (sans tool traces passées).
    Applique compaction si >85% de 128K (via backend.inference.compaction).
    Retourne list[dict] {role, content} prête pour create_chat_completion.
    """
    msgs = load_conversation_history(db, conversation_id)
    # Checkpoint de compaction (q-r.md §matrice) : quand il existe, il ENTRE dans le
    # contexte et remplace l'ancien couvert — les deux modèles le reçoivent via history.
    rich = [{"id": m.id, "role": m.role, "content": m.content or ""}
            for m in msgs if m.role in ("user", "assistant")]
    try:
        from backend.inference.compaction import apply_snapshot_to_history, get_active_snapshot
        trimmed, checkpoint = apply_snapshot_to_history(
            rich, get_active_snapshot(conversation_id))
    except Exception:
        trimmed, checkpoint = rich, None
    # Convertit en format chat (user/assistant uniquement)
    history = [{"role": m["role"], "content": m["content"]} for m in trimmed]
    if checkpoint:
        history = [{"role": "system", "content": checkpoint}] + history
    # Compaction si besoin (Phase 1 : seulement historique partagé)
    try:
        from backend.inference.compaction import should_compact, compact_history

        total = sum(len(m["content"]) // 4 for m in history)
        if should_compact(total, max_tokens):
            # compact_history attend un llm — on ne compacte ici que prune, résumé fait dans loop si manager dispo
            # Pour repo, on prune seulement les vieux tool résultats (défense)
            from backend.inference.compaction import prune_old_tool_results

            history = prune_old_tool_results(history)  # type: ignore
    except Exception:
        pass
    return history


__all__ = [
    "append_message",
    "count_messages",
    "create_conversation",
    "delete_conversation",
    "get_conversation",
    "get_orchestrator_context",
    # `list_conversations` et `rename_conversation` ne sont plus exportés :
    # la responsabilité a migré vers backend/ipc/base.py + services/corbeille.py
    # (garde `_require_conversation`, `deleted_at IS NULL`). Les deux versions
    # legacy ci-dessus sont révolues — ne pas les réexporter.
    "list_messages",
    "load_conversation_history",
    "purge_messages_for_conversation",
]
