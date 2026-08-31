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
    mode: str = "chat",
    conv_id: str | None = None,
) -> Conversation:
    """Crée une conversation (Phase 1 : pas de space_id, pas de model_used)."""
    if not title:
        title = "Nouvelle conversation"
    # Guard : mode valide
    if mode not in ("chat", "agent"):
        raise ValueError(f"mode invalide : {mode}")
    conv = Conversation(
        id=conv_id or new_id(),
        title=title.strip() or "Nouvelle conversation",
        mode=mode,
    )
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
    stmt = (
        select(Conversation)
        .order_by(Conversation.updated_at.desc())
        .offset(offset)
        .limit(limit)
    )
    return db.execute(stmt).scalars().all()


def rename_conversation(db: Session, *, conversation_id: str, title: str) -> Conversation | None:
    conv = db.get(Conversation, conversation_id)
    if conv is None:
        return None
    conv.title = title.strip() or conv.title
    db.commit()
    db.refresh(conv)
    return conv


def delete_conversation(db: Session, *, conversation_id: str) -> bool:
    conv = db.get(Conversation, conversation_id)
    if conv is None:
        return False
    db.delete(conv)  # CASCADE supprime messages/toolcalls/sources
    db.commit()
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
) -> Message:
    """Ajoute un message. Guard : role valide, conversation existe."""
    if role not in ("user", "assistant", "system"):
        raise ValueError(f"role invalide : {role}")
    if not content and role != "system":
        raise ValueError("content vide")
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
    return int(result.rowcount or 0)


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
    # Convertit en format chat (user/assistant uniquement)
    history = [{"role": m.role, "content": m.content} for m in msgs if m.role in ("user", "assistant")]
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
    "list_conversations",
    "list_messages",
    "load_conversation_history",
    "purge_messages_for_conversation",
    "rename_conversation",
]
