# Pseudo-code — Moteur de recherche (Phase 4)

> Phase 4. Ce fichier couvre la **cascade de recherche**, les **fiches de fichiers**, le **cache web** et le **deep research**.

---

## 1. Cascade de recherche

```python
[Phase 4]
# research.py — politique "local d'abord, réseau ensuite".
def resolve_information(question, conversation):
    # Niveau 1 : le contexte suffit ➜ pas d'outil (géré par l'orchestrateur directement).

    # Niveau 2 : documents joints de la discussion.
    facts = search_attachments(conversation, question)
    if facts.sufficient:
        return {"level": 2, "facts": facts}

    # Niveau 3 : recherche web simple.
    hits = web_search(simple_query(question))
    cache_pages(hits)                        # ➜ archive pour ne pas refaire la même recherche
    return {"level": 3, "hits": hits}
```

> **Principe de limitation** : si l'information est dans un PDF joint, on la prend dans le PDF et on **n'émet aucune requête réseau**.

---

## 2. Fiches descriptives des documents

```python
[Phase 4]
# ingest.py — à l'ingestion d'un document, produire une "fiche" compacte.
def build_document_card(path):
    return {
        "title": extract_title(path),
        "summary": summarize(path, sentences=2),   # résumé en 2 phrases
        "keywords": extract_entities(path),        # entités / mots-clés
        "recommendation": infer_usage(path),       # usage recommandé
    }
# NOTE : ces fiches sont injectées dans le prompt de l'orchestrateur (table des matières)
# pour qu'il sache s'il doit ouvrir le fichier SANS relire son contenu.
```

---

## 3. Cache de pages web (fichiers fantômes)

```python
[Phase 4]
def cache_pages(hits):
    for h in hits:
        digest = sha256(h.url)
        if cache_has(digest):       # ⚠️ on ne retélécharge pas une page déjà vue
            continue
        text = trafilatura.extract(fetch(h.url))     # nettoyage HTML → texte
        save_ghost_page(url=h.url, sha256=digest, text=text)
# NOTE : "fichier fantôme" = invisible pour l'utilisateur, réutilisable par le modèle.
# Une question ultérieure sur un sujet déjà exploré consulte ce cache avant tout appel réseau.
```

---

## 4. Deep research

```python
[Phase 4]
#[réservé aux demandes explicites d'analyse complète]
def deep_research(question):
    sub_questions = generate_sub_questions(question)   # formulation de sous-questions
    findings = []
    for sq in sub_questions:
        hits = web_search(sq)
        pages = fetch_and_clean(hits)                  # exploration de liens
        findings.append(extract_facts(pages))
        cache_pages(hits)
    return synthesize_report(findings)                  # rapport structuré + citations
# ⚠️ coût élevé (multi-rebonds) : ne l'activer QUE sur demande explicite.
```

---

## Points de vigilance

- **Ordre strict** : session → documents → (espace, en phase 5) → web simple → deep research.
- **Cache avant réseau** : toujours vérifier le cache avant d'émettre une requête.
- **Citer les sources** : la recherche simple renvoie les URL dans la réponse.

---

## Correspondance

- [Document — Phase 4](../phase_4_moteur_recherche.md)
- Annexe : [07 — Agents, recherche et mémoire](../annexes/07_recherche_agents_memoire.md)
- Suite : [backend_espaces.md](backend_espaces.md)