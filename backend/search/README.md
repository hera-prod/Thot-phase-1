# Recherche documentaire — moteur local (décision interne)

> Note : les renvois `docs/...`, `q-r.md` et à la « Bible » désignent la documentation interne du projet ; elle n'est pas incluse dans l'export HuggingFace.

Décision d'architecture interne (le mémoire d'origine, « annexe 11 », n'est pas livré
dans l'export ; aucune référence à un dossier `docs/` du dépôt).
Le LLM est un utilisateur guidé du moteur, jamais le moteur lui-même.
Phase 1 = FTS5 (document + fragment) + ColBERT late interaction, fusionnés par RRF.

## 1. Dossiers englobés

```
backend/search/              ← CE DOSSIER (Lots A + B + C livrés)
├── __init__.py
├── scope.py                 ← resolve_allowed_artifacts() : liens actifs AVANT tout retrieval
├── cards.py                 ← ensure_card_on_import() + revise_card_after_observation() + aperçu texte
├── observations.py          ← record_tool_observation() : écriture systématique, idempotente par empreinte
├── fts.py                   ← FTS5 + ranking déterministe + fusion hybride RRF (Lot C, même API)
├── reader.py                ← get_card_payload() / read_payload() : fiche et extrait, jamais de chemin (Lot B)
├── trash.py                 ← corbeille 7j + purge GC sécurisé (Lot B, T-SRCH-13/14)
├── chunks.py                ← découpage borné + contextualisation + ingestion (Lot C)
├── vectors.py               ← ColBERT late interaction + fusion RRF (Lot C)
└── (tests) tests/backend/test_search.py (Lot A) + test_search_bc.py (Lots B/C)

Branches :
├── backend/tools/search_documents.py + read_document.py (façades Pydantic)
├── backend/tools/registry.py         ← registre à 10 outils
├── backend/inference/loop.py         ← exécuteurs + record après chaque tool
├── backend/prompts/orchestrator.py   ← workflow chercher→choisir→ouvrir→lire, fiche≠preuve, anti-injection
└── backend/db/ (models.py + database.py) ← colonnes de la décision interne + ArtifactChunk + migration + DDL FTS/vecteurs
```

## 2. Architecture réelle : FTS5 + ColBERT, fusion RRF

La recherche de Phase 1 ne repose **pas** sur le seul FTS5, et n'utilise **pas** de "vecteurs déterministes" (frappe lexicale signée). Elle combine trois signaux, chacun servi par sa propre voie, fusionnés par Reciprocal Rank Fusion (RRF, k=60) :

| signal | voie | répond à |
|---|---|---|
| FTS document (BM25) | `fts.py` sur `artifacts` / `artifact_cards` (index FTS `artifact_search`) | « quel nom contient ce fichier ? » |
| FTS par fragment (BM25) | `fts.py` sur `artifact_chunks` (index FTS `chunk_search`) | « quels mots contient ce passage ? » |
| ColBERT (late interaction) | `vectors.py` `search_vectors()` | « de quoi parle ce fragment, quels que soient les mots ? » |

**ColBERT** (`backend/search/vectors.py`) : un modèle `LFM2.5-ColBERT-350M` chargé **une fois, résident** (685 Mio VRAM en bf16, jamais déchargé entre deux requêtes). Contrairement à un encodeur de phrase qui réduit un document à un seul vecteur, ColBERT conserve **un vecteur par jeton** (128 dimensions par jeton, projection 1024→128 via la tête dense `1_Dense`). Le score est un MaxSim :

```
score(requête, document) = Σ_sur_jetons_de_la_requête  max_sur_jetons_du_document(q_i · d_j)
```

Chaque jeton de la requête choisit le jeton du document qui lui ressemble le plus ; la somme de ces meilleurs appariements classe. Cela permet d'apparier « livraison » (document) avec « expédier » (requête) là où un vecteur unique par phrase se noie.

Le format persistant est auto-descriptif (en-tête `THOTC`, dim=128, nb de jetons) : un ancien vecteur de frappe lexicale (256 flottants, aucun en-tête) est refusé par `unpack_vectors`. Rien ne se réindexe tout seul : `detecter_vecteurs_obsoletes()` constate en lecture seule, et `reindexer_vecteurs(..., appliquer=False)` par défaut ne réécrit rien tant qu'on ne demande pas explicitement `appliquer=True`. La requête est encodée avec le préfixe `[Q] `, le document avec `[D] `.

**Fusion** : `fts.py` appelle `rrf_fuse()` sur les listes classées (FTS document, FTS fragment, ColBERT) pour produire le classement final. Le filtre de portée (`scope.py`, liens actifs de la conversation) est appliqué **dans la lecture SQL** avant tout décodage de vecteur — un artefact hors périmètre n'est jamais décodé, donc jamais compté, donc jamais susceptible d'influer sur un rang.

## 3. Pseudo-code (comment c'est fait)

```
# Import (Electron crée Artifact/ArtifactLink ; Python assure la fiche en paresseux)
SUR premier contact avec artifact SANS fiche courante :
  ensure_card_on_import(artifact) :
    fiche rev 1 = {title, summary, keywords, document_type, capabilities, analysis_status: "pending", is_current: true}
    indexer la fiche (fts.index_card)

# Après chaque tool (vision/doc_ocr/read_text/read_document/search_documents)
record_tool_observation(artifact_id, tool, query, result, run_id, tool_call_id) :
  Observation IMMUABLE {input_fingerprint: sha(...), status, excerpt}
  revise_card_after_observation(...) : fiche N+1, N.is_current=false, N+1.is_current=true, réindexer

# Recherche (exécuteur search_documents)
execute_search_documents(args, run_scope) :
  valider Pydantic (query 1..300, limit 1..20, cursor opaque)
  allowed = resolve_allowed_artifacts(conv_id)          # LIENS ACTIFS D'ABORD
  SI artifact demandé hors allowed → ARTIFACT_NOT_IN_SCOPE (sans confirmer l'existence)
  normaliser query (NFC, minuscules, diacritiques) MAIS journaliser l'originale
  fts_document  = FTS5 MATCH(filename/title/summary/keywords/entities/preview)   → liste classée
  fts_fragment  = FTS5 MATCH sur artifact_chunks                                   → liste classée
  colbert       = search_vectors(session, query, allowed_ids)                      → MaxSim → liste classée
                 (le moteur est sondé AVANT toute lecture SQL ; s'il est absent,
                  le FTS lexical reste servi et statut_moteur() dit pourquoi)
  fusion = rrf_fuse([fts_document, fts_fragment, colbert], k=60)
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
    question ──▶ search_documents ─────┬──▶ FTS5 document (BM25) ──┐
                                       │                           │
                                       ├─▶ FTS5 fragment (BM25) ───┼─▶ rrf_fuse (k=60) ──▶ classement final
                                       │                           │
                                       └─▶ ColBERT MaxSim ─────────┘
              (128 dim/jeton, un vecteur par jeton, résident 685 Mio)
                                         │
                                         ▼
                                read_document (fiche + extrait borné, jamais de chemin)
                                         │
                                         ▼
                                read/vision/ocr ciblé → Observation → fiche N+1
                                         │
                                         ▼
                         brief + evidence_ids ──▶ Générateur ──▶ réponse
```

## 5. Tests (T-SRCH — Lots A + B + C livrés)

Lot A (`test_search.py`) : `01` fiche minimale · `02` OCR→observation→fiche N+1 · `03` historique ·
`04` sans accents · `05` filtres · `06` scope strict · `07` id deviné refusé ·
`09` zéro borné · `10` raisons de match · `15` ColBERT présent · `17` parité · curseur · lecture limitée portée par `read_document` (`get_source` retiré).
Lot B/C (`test_search_bc.py`) : `08` volume 300 + pagination stable · `11` read_document→evidence
(`page:N` OCR, `offset:N`)/`12` injection→donnée seule · `13` corbeille/restore · `14` purge GC ·
`16` droits AVANT hybride · hybride sans régression · workspace prêt (couche scope/fts, actif Phase 5).

## 6. Pièges

- `working_path` ne sort JAMAIS vers le LLM (résolution blob→chemin reste privée aux exécuteurs).
- Curseur d'une autre recherche → `SEARCH_CURSOR_MISMATCH`, relancer sans curseur.
- Fiche = aide à la découverte, jamais preuve primaire (le générateur cite Evidence/extraits).
- Indexation = tâche de fond basse priorité ; chat interactif prioritaire (admission existante).
- Evidence sans table dédiée : `obs_…` (extrait d'analyse) ou `blob:{sha}:chars:a-b` (texte brut).
- Workspace : couches scope/fts prêtes ; l'exécuteur refuse `scope=workspace` tant que les espaces
  (Phase 5) ne fournissent pas le workspace du run.
- Hybride : ingestion chunks bornée (50/artefact, textes vides ignorés) ; fusion auto seulement si
  chunks indexés dans le scope, sinon lexical pur. Reranker = blend déterministe, pas un LLM.
