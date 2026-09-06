# Pseudo-code — Boucle agentique

> Phase 1. Ce fichier couvre la **boucle en deux couches** (orchestrateur → générateur), le **double historique**, le **sanitizer** et la **compaction**.

---

## 1. La boucle principale

```python
[Phase 1]
# loop.py — un tour de discussion = orchestration PUIS génération.
def run_turn(user_message, conversation):
    # --- Couche 1 : orchestration ---
    brief, tool_results = orchestrate(user_message, conversation)   # l'orchestrateur peut appeler des outils

    # --- Couche 2 : génération ---
    final_answer = generate(brief, tool_results)                    # le générateur rédige, SANS outil

    # ⚠️ On ne persiste QUE le dialogue visible (user + assistant), jamais les traces d'outils.
    save_message(role="assistant", content=final_answer)
    return final_answer
```

---

## 2. L'orchestrateur

```python
[Phase 1]
def orchestrate(user_message, conversation):
    # NOTE : max 5 itérations, délai par appel d'outil (ex. 15s), séquentiel.
    for _ in range(MAX_ITER):
        response = call_llm(role="orchestrator", tools=registry.definitions(),   # ⚠️ SEUL l'orchestrateur reçoit les outils
                            messages=history_visible(conversation) + [user_message])
        tool_calls = extract_tool_calls(response)
        if not tool_calls:
            return response, []             # pas d'outil ➜ on passe au générateur
        results = []
        for call in tool_calls:
            emit_event("tool_call", {"tool": call.name})
            # ➜ l'UI affiche l'état "en attente" de l'outil
            result = execute_tool(call, manager)     # swap modèle si besoin (voir backend_outils)
            emit_event("tool_result", {"tool": call.name, "result": result})
            results.append(result)
    # si trop d'itérations ➜ forcer la génération avec ce qu'on a
```

---

## 3. Le générateur

```python
[Phase 1]
def generate(brief, prepared_results):
    # NOTE : le générateur N'A PAS d'outils. Il produit la réponse finale propre.
    messages = [system_generator, brief, tool_observations(prepared_results)]
    return stream_llm(role="generator", messages=messages)   # émet des événements "answer"
```

---

## 4. Double historique

```python
[Phase 1]
# NOTE : l'historique partagé ne contient QUE user↔assistant.
# Les tool_results du tour courant sont gardés EN MÉMOIRE uniquement (pas persistés).
def history_visible(conversation):
    # ➜ charge uniquement les messages role in (user, assistant)
    # ⚠️ ne réinjecte NI le "thinking" NI les appels d'outils des tours précédents.
```

> **Pourquoi ?** Réinjecter les traces de raisonnement des tours passés ajoute du **bruit** et fait déraper le modèle. On garde le dialogue propre, et on ne transmet les résultats d'outils que pour le tour en cours.

---

## 5. Sanitizer (nettoyage des fuites)

```python
[Phase 1]
# ⚠️ Le modèle peut "fuiter" son raisonnement (<thinking>...) ou un appel d'outil textuel.
def sanitize(content):
    content = strip_thinking_blocks(content)       # retire <thinking>...</thinking>
    content = strip_textual_tool_calls(content)    # retire [vision("...")] textuel
    return content
# NOTE : appliquer côté backend AVANT de persister, pour garantir que la base et
#       l'historique du tour suivant restent propres (le front le fait aussi, en défense).
```

---

## 6. Préparation des résultats d'outils

```python
[Phase 1]
def prepare_tool_results(results, llm):
    # NOTE : on borne la taille injectée au générateur pour ne pas saturer le contexte.
    # ≤ 8k jetons ➜ brut ; 8-16k ➜ tronqué ; > 16k ➜ résumé LLM (~500 mots).
    for r in results:
        if tokens(r) <= 8000:  keep(r)
        elif tokens(r) <= 16000:  truncate(r)
        else:  summarize(r)
```

---

## 7. Compaction de contexte

```python
[Phase 1]
def maybe_compact(conversation):
    # NOTE : quand on approche ~110k jetons (sur 128k), on résume l'historique.
    if context_tokens(conversation) < COMPACTION_THRESHOLD:
        return
    summary = call_llm(role="compactor", prompt=COMPACTION_PROMPT,
                       messages=history_visible(conversation))
    # ➜ le résumé REMPLACE l'historique détaillé comme nouveau contexte.
    replace_context(conversation, summary)
```

> **Point clé** : la compaction s'appuie sur **l'historique visible** (dialogue), pas sur les traces d'orchestration. Elle préserve les faits et chiffres, pas le verbatim.

---

## Correspondance

- [Document — Phase 1](../phase_1_socle_et_vision.md)
- Suite : [backend_outils.md](backend_outils.md)