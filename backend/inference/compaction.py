"""
Compaction Opencode-inspired — ne compacte que l'historique partagé user↔assistant
Notes.md § Double historique

But : éviter de perdre le fil dans les longues conversations (>100K tokens) sans exploser le 128K.
- On ne compacte JAMAIS les tool traces (ils sont éphémères)
- On garde les 15K tokens récents intacts, on résume seulement l'ancien
"""
PRUNE_PROTECT = 40_000
COMPACT_THRESHOLD = 0.85
PRESERVE_RECENT = 15_000

SYSTEM_COMPACTION = """Tu es Thot Compaction (LFM2.5-8B).
Crée un résumé détaillé pour continuer la conversation.

Inclus:
- Ce qui a été accompli (faits, décisions)
- Travail en cours (état actuel)
- Fichiers/ressources impliqués
- Prochaines étapes claires
- Contraintes/préférences utilisateur
- Décisions techniques importantes

Sois concis mais complet : ce résumé sera le SEUL contexte ancien disponible."""

def count_tokens(text: str) -> int:
    return len(text) // 4

def should_compact(history_tokens: int, max_tokens: int = 128_000) -> bool:
    return history_tokens / max_tokens >= COMPACT_THRESHOLD

def prune_old_tool_results(messages: list[dict]) -> list[dict]:
    total = 0
    for i in range(len(messages)-1, -1, -1):
        if messages[i].get("role") == "tool":
            total += count_tokens(messages[i].get("content",""))
            if total > PRUNE_PROTECT:
                messages[i] = {**messages[i], "content": "[Ancien résultat outil effacé]"}
    return messages

def compact_history(messages: list[dict], llm) -> list[dict]:
    messages = prune_old_tool_results(messages[:])

    # split recent
    recent_tokens = 0
    split_idx = len(messages)
    for i in range(len(messages)-1, -1, -1):
        t = count_tokens(messages[i].get("content",""))
        if recent_tokens + t <= PRESERVE_RECENT:
            recent_tokens += t
            split_idx = i
        else:
            break

    old = messages[:split_idx]
    recent = messages[split_idx:]

    if not old:
        return messages

    conv = "\n\n".join(f"[{m['role']}]: {m['content']}" for m in old)
    summary = llm.create_completion(prompt=f"{SYSTEM_COMPACTION}\n\nConversation:\n{conv}", max_tokens=2048, temperature=0.3)["choices"][0]["text"].strip()

    return [{"role": "system", "content": f"[Résumé conversation]: {summary}"}] + recent
