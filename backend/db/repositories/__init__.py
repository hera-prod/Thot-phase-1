# db/repositories — exports Phase 1 (simplifié, 4 tables)
#
# `list_conversations` et `rename_conversation` ne sont plus réexportés :
# leur responsabilité a migré vers backend/ipc/base.py via services/corbeille.py
# (garde `_require_conversation`, `deleted_at IS NULL`). Les versions legacy du
# dépôt sont révolues — ne pas les ressusciter dans les exports.
from backend.db.repositories.conversations import (
    append_message,
    count_messages,
    create_conversation,
    delete_conversation,
    get_conversation,
    list_messages,
    load_conversation_history,
)

__all__ = [
    "append_message",
    "count_messages",
    "create_conversation",
    "delete_conversation",
    "get_conversation",
    "list_messages",
    "load_conversation_history",
]
