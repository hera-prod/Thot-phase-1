# DB & Persistance — SQLite + blobs durables

> Note : les renvois `docs/...`, `q-r.md` et à la « Bible » désignent la documentation interne du projet ; elle n'est pas incluse dans l'export HuggingFace.

## 1. Dossiers englobés

```
backend/db/
├── models.py            ← 25 tables métier SQLAlchemy + schema_migrations + 2 tables FTS (artifact_search, chunk_search) ; source de vérité schéma
├── database.py          ← init_db + SessionLocal, lit THOT_DB_PATH / THOT_DATA_DIR en priorité
├── media_cache.py       ← legacy compat : écrit à plat data/blobs/<sha256><ext> (pipeline image_paths → table sources) ; vignettes reconstructibles (jamais stockées)
└── repositories/
    └── conversations.py ← list_conversations, list_messages (+attachments[]), persistance tours
```

Source vérité des chemins : `electron/paths.cjs` (`resolveDataDir` : portable `<racine>/data`
si `portable.json`, sinon XDG `~/.local/share/thot/data`). Electron (`electron/sidecar.cjs`)
spawn le sidecar Python avec `THOT_DATA_DIR / THOT_DB_PATH / THOT_APP_DATA_DIR` en env —
Python les lit en priorité, sinon `get_data_dir()/database/thot-phase-1.db`. SQLite `WAL` + `foreign_keys=ON`.

L'historique (`list_conversations`, `list_messages`) est servi par le sidecar Python
(`backend/ipc/base.py` → `backend/db/repositories/conversations.py`), invoqué par le frontend
via l'IPC Electron (`thot:cmd`). Ces méthodes vivaient jadis en `rusqlite`, dans la couche
native d'avant Electron ; aujourd'hui le processus Electron ne touche jamais la base
directement (plus de `python3 -c` non plus).

## 2. Pseudo-code (comment c'est fait)

```
# Cycle de vie fichier (annexe 10)
staging (volatil)  : data/temp/staged/{hash}.ext  + stage_item_id (sti_...)
  → attachments_prepare (AVANT le run) :
durable            : data/blobs/<2 hex>/<sha256>.bin (shardé par 2 hex, dédupliqué par sha256, jamais renommé)
  + Artifact{id: art_..., blob_id, kind, original_filename}
  + ArtifactLink{artifact_id, scope_kind: conversation|workspace|library, scope_id}
  + MessageAttachment{message_id, artifact_id, ordinal}  # ordre d'envoi, survit la corbeille
  + vignette         : reconstruite à la demande (objectURL frontend), jamais source de vérité
corbeille (état SQL, 7 jours — backend/search/trash.py, aucun dossier data/trash)
  → purge retire ArtifactLink puis GC blob si orphelin

# Persistance tour chat (backend/main.py + repositories)
SUR chat.send :
  transaction 1 : Conversation (get-or-create) + Message user + MessageAttachment[ordinal]
  ... run LLM (loop) ...
  transaction 2 : Message assistant + ToolCall[] + Observation[] + Source[] + Run{status, fingerprint, seq}
  PUIS SEULEMENT emit terminal (completed) — jamais de terminal avant persistance (C08)

# Tables (25 métier + schema_migrations + 2 FTS artifact_search/chunk_search)
conversations / messages / toolcalls / sources (legacy media/ enrichie) /
runs (C09/C11 : request_id, fingerprint, seq) /
context_snapshots (C06 : revision, covered_through_message_id, summary) /
blobs (sha256 unique, relative_path, size, mime, state) /
artifacts / artifact_links (scope C18) / message_attachments (ordre 10:125) /
artifact_cards (résumé par révision) / observations (10:441 : artifact_id, tool, status, payload_ref)

Trace orchestrateur (bulle UI) : messages.orchestrator_thinking + brief + plan_json
(handoff exact, persistés AVANT terminal) ; toolcalls.run_id écrit pendant le run,
backfillé vers msg_id par main.py. `list_messages` (servi par le sidecar) remonte trace + toolcalls
par message → la bulle se reconstruit au reload, jamais vide.

# Lecture historique UI (09/09)
list_messages(conv_id) → messages + attachments[] (artefacts + sources legacy, ids seuls)
get_attachment_bytes(artifact_id) → bytes bruts → frontend reconstruit vignette (objectURL)
  + chips 📄 pour PDF (pas de pari asset://)
```

## 2bis. Schémas ASCII

```
CYCLE DE VIE FICHIER (volatil → durable → corbeille → GC)
─────────────────────────────────────────────────────
data/temp/staged/{hash}.ext           data/blobs/<2 hex>/<sha256>.bin
  (VOLATIL, sti_...)                    (DURABLE, shardé 2 hex, dédupliqué sha256)
        │                                        │
        │  attachments_prepare (AVANT le run)    │
        └──────────────┬─────────────────────────┘
                       ▼
              ┌────────────────┐
              │ Artifact art_… │── blob_id, kind, original_filename
              └───────┬────────┘
                      │ ArtifactLink (scope C18)
                      │ conversation │ workspace │ library
                      ▼
              ┌──────────────────┐
              │ MessageAttachment│── (message_id, artifact_id, ordinal)
              │ ordre d'envoi    │
              └──────────────────┘
                       │ 7 jours en corbeille (état SQL)
                       ▼
              purge ArtifactLink → GC blob SI orphelin (sinon gardé)
              vignettes = reconstruites à la demande (jamais source de vérité)
```

```
RELATIONS (simplifié — 25 tables en base)
───────────────────────────────
conversations 1───* messages 1───* toolcalls
     │                  │
     │                  ├───────* sources (legacy enrichie)
     │                  │
     │                  └───────* message_attachments *───1 artifacts
     │                                                        │
     ├──────────* runs (request_id, fingerprint C11, seq C08) │
     ├──────────* context_snapshots (revision C06)            │
     │                                                        ├───────────* artifact_links (scope C18)
     │                                                        ├───────────* artifact_cards (résumé/révision)
     │                                                        └───────────* observations (tool,status,payload_ref)
     │
blobs 1───* artifacts (sha256 unique — deux artifacts peuvent partager un blob)

ORDRE PERSISTANCE (C08 : terminal APRÈS persistance, jamais avant)
  T1 (avant LLM) : Conversation + Message[user] + MessageAttachment
  T2 (après LLM) : Message[assistant] + ToolCall[] + Observation[] + Run{completed}
  PUIS emit terminal. Un seul terminal par run_id.
```

## 3. Extraits réels

```bash
sqlite3 ~/.local/share/thot/data/database/thot-phase-1.db ".tables"  # 25 tables métier + schema_migrations + 2 tables FTS (artifact_search, chunk_search)
sqlite3 ~/.local/share/thot/data/database/thot-phase-1.db "SELECT id FROM blobs LIMIT 3;"
```

```python
# backend/db/database.py — priorité env Electron
db_path = os.environ.get("THOT_DB_PATH") or get_data_dir() / "database" / "thot-phase-1.db"
```

Scope (garde-fou) : outil sur `artifact_id` hors conv → `ATTACHMENT_NOT_IN_SCOPE` (C18).

## 4. Pièges

- `data/temp/staged` = volatil (peut être purgé) ; seul `data/blobs` est durable.
- Aucun dossier `data/previews/` ni `data/trash/` : les vignettes sont reconstruites à la demande et la corbeille est un état SQL (7 jours, `backend/search/trash.py`). Ne pas les réinventer comme dossiers. **Layout réel des blobs** — chaque écriture range le fichier sous sa propre convention et enregistre le chemin exact dans `blobs.relative_path` ; TOUTES les lectures résolvent `get_data_dir() / relative_path` sans jamais recalculer le sharding, donc les conventions coexistent sans casse :
  - `data/blobs/<2 hex>/<sha256>.bin` — dépôt pièces jointes (`backend/ipc/attachments.py:1025,1056`) et ressources web (`backend/ingestion/sources_web.py:220-230`) ;
  - `data/blobs/sha256/<2 hex>/<sha256>.bin` — images générées (`backend/services/flux_spike.py:1127-1132`) ;
  - `data/blobs/<sha256><ext>` à plat — compat historique `image_paths` (`backend/db/media_cache.py:29`, table legacy `sources`, prefixe `media/` dans `relative_path`).
  **Écarts connus à corriger** (décrits honnêtement, non réparés) :
  1. `backend/main.py:875-979` (compat `image_paths`) crée une ligne `blobs` pointant vers `blobs/<2 hex>/<sha256>.bin` sans jamais copier les octets à cet emplacement : le fichier reste dans le staging puis est purgé → `depot_integrite` signale `fichier_absent`, l'artefact devient illisible.
  2. Un même contenu passé par deux pipelines (ex. flux puis dépôt) laisse deux fichiers pour une seule ligne `blobs` — l'autre fichier est signalé orphelin par `depot_integrite._blob_orphelin` puis purgé (bruit d'intégrité, pas de perte).
- `image_paths` legacy → promotion hash→blob avec log `compat`, puis `artifact_id`.
