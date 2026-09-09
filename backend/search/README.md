# Recherche documentaire — moteur local (annexe 11)

Cible : `Depot_Github/annexes/11_moteur_recherche_fichiers_toolcalls_llm.md`.
Le LLM est un utilisateur guidé du moteur, jamais le moteur lui-même.
Phase 1 = FTS5 + filtres, zéro embedding obligatoire.

## 1. Dossiers englobés

```
backend/search/              ← CE DOSSIER (Lot A)
├── __init__.py
├── scope.py                 ← resolve_allowed_artifacts() : liens actifs AVANT tout retrieval
├── cards.py                 ← ensure_card_on_import() + revise_card_after_observation()
├── observations.py          ← record_tool_observation() : écriture systématique post-outil
├── fts.py                   ← table FTS5 + index_card() + fts_search() + ranking déterministe
└── (tests) backend/tests/test_search.py  ← T-SRCH-01..17 (périmètre Lot A, voir §5)

Branches :
├── backend/tools/search_documents.py ← SearchDocumentsTool (découverte bornée, paginée)
├── backend/tools/registry.py         ← registre à 5 outils
├── backend/inference/loop.py         ← exécuteur search + record après chaque tool + get_source sans chemin
├── backend/prompts/orchestrator.py   ← workflow chercher→choisir→ouvrir→lire, fiche≠preuve
└── backend/db/ (models.py + database.py) ← colonnes annexe 11 + migration + DDL FTS5
```

## 2. Mapping annexe 11 → existant → Lot A

```
Concept annexe 11              Existant Phase 1                    Lot A (ce dossier)
─────────────────────────────  ──────────────────────────────────  ─────────────────────────────
Blob / Artifact /              models.py OK, créés par            inchangé (source de vérité)
  ArtifactLink /                 Rust attachments_prepare
  MessageAttachment
ArtifactCard versionnée        modèle minimal (revision,           colonnes annexe 11 (title,
  (is_current, title,             summary, capabilities,             keywords/entities, doc_type,
   keywords, status...)           quality, status)                   analysis_status, is_current...)
  → ensure_card_on_import      ABSENT                              cards.py (lazy self-healing :
  → revise après observation   ABSENT                               Rust crée l'Artifact, Python
                                                                    assure la fiche au 1er contact)
Observation persistée          modèle minimal, JAMAIS               observations.py : écriture
  après chaque tool              écrite (résultats en               systématique vision/doc_ocr/
                                 mémoire du tour seulement)         read_text/get_source/search
Index FTS5                     ABSENT                              fts.py (unicode61 sans
                                                                    diacritiques, fiche active
                                                                    + aperçu borné, jamais blob)
search_documents               ABSENT                              tools/search_documents.py
  (scope, types, status,                                          (query≤300, limit≤20,
   limit≤20, cursor)                                               scope=conversation Lot A)
get_source compat limitée      list-all TOUT (≤50) +               ≤20 récents + renvoi vers
                                 working_path AU LLM               search_documents ; métadonnées
                                                                   SANS working_path au LLM
Scope-check avant retrieval    C18 partiel (vision artifact)       scope.py : allowed AVANT FTS,
                                                                   ARTIFACT_NOT_IN_SCOPE sans
                                                                   révéler l'existence
```

## 3. Pseudo-code Lot A

```
# Import (Rust crée Artifact/ArtifactLink ; Python assure la fiche en paresseux)
SUR premier contact avec artifact SANS fiche courante :
  ensure_card_on_import(artifact) :
    fiche rev 1 = {title: nom fichier, summary: "importé ; non analysé",
                   keywords: [stem du nom], document_type: kind,
                   capabilities: ["ocr"] si pdf/image, ["text_search"] si texte,
                   analysis_status: "pending", is_current: true}
    indexer la fiche (fts.index_card)

# Après chaque tool (vision/doc_ocr/read_text/get_source/search_documents)
record_tool_observation(artifact_id, tool, query, result, run_id, tool_call_id) :
  Observation IMMUABLE {input_fingerprint: sha(blob+outil+modèle+params+query), status, excerpt}
  revise_card_after_observation(...) :
    fiche N+1 (titre/summary/keywords prudents : "OCR partiel", "probablement…")
    N.is_current=false, N+1.is_current=true, réindexer (désindexer N, indexer N+1)
  → réutilisable SSI même artifact + même sha + capacité compatible + question couverte

# Recherche (exécuteur search_documents)
execute_search_documents(args, run_scope) :
  valider Pydantic (query 1..300, limit 1..20, cursor opaque)
  allowed = resolve_allowed_artifacts(conv_id)          # LIENS ACTIFS D'ABORD
  SI artifact demandé hors allowed → ARTIFACT_NOT_IN_SCOPE (sans confirmer l'existence)
  normaliser query (NFC, minuscules, diacritiques) MAIS journaliser l'originale
  candidats = FTS5 MATCH(filename/title/summary/keywords/entities/preview)
              FILTRÉ allowed + types + status, max 50
  score = 4×filename + 3×title + 2×keywords/entities + 1.5×summary + 1×extrait + 0.2×récence
  page stable (cursor = base64{query_hash, filtres, scope, offset}) ; 0 résultat = suggestions,
    JAMAIS "le fichier n'existe pas" → "pas retrouvé dans le périmètre consulté"
```

## 4. Schéma ASCII

```
                 ┌──────────────────────────────────────────────┐
                 │  HORS PROMPT (jamais au LLM)                 │
                 │  catalogue · blobs · fiches · index · droits │
                 └──────────────────────┬───────────────────────┘
                                        │ tool_call structuré
                                        ▼
                 ┌──────────────────────────────────────────────┐
                 │ scope.py : allowed = liens actifs (conv)     │ ◀── ÉTAPE 0, avant FTS
                 └──────────────────────┬───────────────────────┘
                                        ▼
   question ──▶ search_documents ──▶ FTS5 (50 max, filtrés) ──▶ score déterministe
      │              (5-10 au LLM)         │ filename×4 > title×3 > kw×2 > summary
      │                                   ▼
      │                            get_document_card (fiche, SANS contenu/chemin)
      │                                   ▼
      │                            read/vision/ocr ciblé → Observation → fiche N+1
      │                                   ▼
      └────────────────────── brief + evidence_ids ──▶ Générateur ──▶ réponse
```

## 5. Tests (T-SRCH, périmètre Lot A)

`01` fiche minimale à l'import · `02` OCR→observation→fiche N+1 · `03` historique fiches conservé ·
`04` FTS sans accents (`electricite`→`électricité`) · `05` filtre types · `06` scope strict 2 convs ·
`07` id deviné hors conv → `ARTIFACT_NOT_IN_SCOPE` · `09` 0 résultat → périmètre + suggestions ·
`10` 3 candidats → raisons de match · `15` sans embeddings · `17` parité prompt/schémas.
Lot B : `08` (10k, pagination), `11` (Evidence), `12` (injection), `13/14` (corbeille/GC), `16` (hybride).

## 6. Pièges

- `working_path` ne sort JAMAIS vers le LLM (résolution blob→chemin reste privée aux exécuteurs).
- Curseur d'une autre recherche → `SEARCH_CURSOR_MISMATCH`, relancer sans curseur.
- Fiche = aide à la découverte, jamais preuve primaire (le générateur cite Evidence/extraits).
- Indexation = tâche de fond basse priorité ; chat interactif prioritaire (admission existante).
