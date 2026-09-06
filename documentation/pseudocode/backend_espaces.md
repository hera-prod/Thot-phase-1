# Pseudo-code — Espaces de travail et mémoire (Phase 5)

> Phase 5. Ce fichier couvre les **espaces**, le **cloisonnement sources/mémoire** et le **référencement par score**.

---

## 1. Modèle d'espace

```python
[Phase 5]
# workspace.py — un espace regroupe des sessions + deux dossiers logiques.
class Workspace:
    id, name
    user_dir      # ⚠️ dossier utilisateur : lecture seule pour l'IA (sanctuaire)
    memory_dir    # dossier mémoire : écrit par l'orchestrateur

# Une discussion appartient à un espace (Session.workspace_id) ;
# un fichier est RÉFÉRENCÉ, jamais copié d'un dossier à l'autre.
```

---

## 2. Cloisonnement des écritures

```python
[Phase 5]
# file_manager.py — garde-fou d'écriture.
def ensure_writable(path):
    # ⚠️ refuse tout accès en écriture sur workspace_user.
    if is_under(path, workspace.user_dir):
        raise ForbiddenWrite("dossier utilisateur en lecture seule")
# NOTE : l'utilisateur peut purger la mémoire sans toucher à ses documents d'origine.
```

---

## 3. Cascade de recherche enrichie (niveau 2.5)

```python
[Phase 5]
# On insère DEUX paliers locaux entre les documents joints et le web :
#   2.5a — dossier utilisateur de l'espace (fichiers partagés)
#   2.5b — mémoire de l'espace (synthèses classées par score)
def resolve_information(question, conversation):
    facts = search_attachments(conversation, question)      # niveau 2
    if facts.sufficient: return facts

    facts = search_workspace_user(workspace, question)      # niveau 2.5a
    if facts.sufficient: return facts

    facts = search_workspace_memory(workspace, question)    # niveau 2.5b
    if facts.sufficient:
        bump_utility_score(facts.card)                       # ➜ incrémente le score (validation)
        return facts

    return web_search(...)                                   # niveaux 3 / 4
```

---

## 4. Référencement par score

```python
[Phase 5]
# memory/ranking.py — score d'utilité = récence + fréquence + validation.
def recency_score(card):      # accès récent ?
def frequency_score(card):    # utilisé souvent ?
def validation_score(card):   # a déjà résolu une requête ?

def bump_utility_score(card):
    card.utility_score += 1            # ➜ incrémenté quand une fiche résout une requête
    card.last_accessed_at = now()

def top_cards(workspace, k=5):
    # NOTE : ne présente au modèle QUE les k meilleures fiches dans sa table des matières.
    # ➜ garde le prompt léger (pas de jeton superflu) tout en privilégiant l'éprouvé.
    return sorted(cards, key=utility_score, reverse=True)[:k]
```

---

## Points de vigilance

- **`workspace_user` ≠ `workspace_memory`** : un seul est écrit par l'IA.
- **Le score classe les fiches**, il ne modifie pas les documents.
- **Partage par référence** : éviter les doublons de fichiers sur disque.
- **Le niveau 2.5 est local** : aucun appel réseau.

---

## Correspondance

- [Document — Phase 5](../phase_5_espaces_et_memoire.md)
- Annexe : [07 — Agents, recherche et mémoire](../annexes/07_recherche_agents_memoire.md)
- Schéma : [backend_db.md](backend_db.md)