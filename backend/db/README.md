# DB & Persistance — SQLite + blobs durables

## 1. Dossiers englobés

```
backend/db/
├── models.py            ← 12 tables SQLAlchemy (source de vérité schéma)
├── database.py          ← init_db + SessionLocal, lit THOT_DB_PATH / THOT_DATA_DIR en priorité
├── media_cache.py       ← accès blobs (data/blobs), previews reconstructibles
└── repositories/
    └── conversations.py ← list_conversations, list_messages (+attachments[]), persistance tours
```

Source vérité des chemins : Rust `resolve_data_dir` (portable `Thot/data` si `portable.json`,
sinon XDG `~/.local/share/thot/data`). Rust spawn le sidecar avec
`THOT_DATA_DIR / THOT_DB_PATH / THOT_APP_DATA_DIR` en env — Python les lit en priorité,
sinon `get_data_dir()/database/thot-phase-1.db`. SQLite `WAL` + `foreign_keys=ON`.

`list_conversations` historique est servi en Rust via `rusqlite` direct (plus de `python3 -c`).

## 2. Pseudo-code (comment c'est fait)

```
# Cycle de vie fichier (annexe 10)
staging (volatil)  : data/temp/staged/{hash}.ext  + stage_item_id (sti_...)
  → attachments_prepare (AVANT le run) :
durable            : data/blobs/sha256/ab/cd...bin (dédupliqué par sha256, jamais renommé)
  + Artifact{id: art_..., blob_id, kind, original_filename}
  + ArtifactLink{artifact_id, scope_kind: conversation|workspace|library, scope_id}
  + MessageAttachment{message_id, artifact_id, ordinal}  # ordre d'envoi, survit trash 7j
  + preview          : data/previews/... (reconstructible, pas source de vérité)
trash 7 jours → purge retire ArtifactLink puis GC blob si orphelin

# Persistance tour chat (backend/main.py + repositories)
SUR chat.send :
  transaction 1 : Conversation (get-or-create) + Message user + MessageAttachment[ordinal]
  ... run LLM (loop) ...
  transaction 2 : Message assistant + ToolCall[] + Observation[] + Source[] + Run{status, fingerprint, seq}
  PUIS SEULEMENT emit terminal (completed) — jamais de terminal avant persistance (C08)

# Tables (12)
conversations / messages / toolcalls / sources (legacy media/ enrichie) /
runs (C09/C11 : request_id, fingerprint, seq) /
context_snapshots (C06 : revision, covered_through_message_id, summary) /
blobs (sha256 unique, relative_path, size, mime, state) /
artifacts / artifact_links (scope C18) / message_attachments (ordre 10:125) /
artifact_cards (résumé par révision) / observations (10:441 : artifact_id, tool, status, payload_ref)

# Lecture historique UI (09/09)
list_messages(conv_id) → messages + attachments[] (artefacts + sources legacy, ids seuls)
get_attachment_bytes(artifact_id) → bytes bruts → frontend reconstruit vignette (objectURL)
  + chips 📄 pour PDF (pas de pari asset://)
```

## 2bis. Schémas ASCII

```
CYCLE DE VIE FICHIER (volatil → durable → trash → GC)
─────────────────────────────────────────────────────
data/temp/staged/{hash}.ext          data/blobs/sha256/ab/cd...bin
  (VOLATIL, sti_...)                   (DURABLE, dédupliqué sha256)
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
                       │ 7 jours en trash
                       ▼
              purge ArtifactLink → GC blob SI orphelin (sinon gardé)
              previews/ = reconstructible (jamais source de vérité)
```

```
RELATIONS 12 TABLES (simplifié)
───────────────────────────────
conversations 1───* messages 1───* toolcalls
     │                  │
     │                  ├───────* sources (legacy enrichie)
     │                  │
     │                  └───────* message_attachments *───1 artifacts
     │                                                        │
     ├──────────* runs (request_id, fingerprint C11, seq C08) │
     ├──────────* context_snapshots (revision C06)            │
     │                                                        ├ dix-sept───* artifact_links (scope C18)
     │                                                        ├ dix-sept───* artifact_cards (résumé/révision)
     │                                                        └ dix-sept───* observations (tool,status,payload_ref)
     │
blobs 1───* artifacts (sha256 unique — deux artifacts peuvent partager un blob)

ORDRE PERSISTANCE (C08 : terminal APRÈS persistance, jamais avant)
  T1 (avant LLM) : Conversation + Message[user] + MessageAttachment
  T2 (après LLM) : Message[assistant] + ToolCall[] + Observation[] + Run{completed}
  PUIS emit terminal. Un seul terminal par run_id.
```

## 3. Extraits réels

```bash
sqlite3 ~/.local/share/thot/data/database/thot-phase-1.db ".tables"  # 12 tables
sqlite3 ~/.local/share/thot/data/database/thot-phase-1.db "SELECT id FROM blobs LIMIT 3;"
```

```python
# backend/db/database.py — priorité env Rust
db_path = os.environ.get("THOT_DB_PATH") or get_data_dir() / "database" / "thot-phase-1.db"
```

Scope (garde-fou) : outil sur `artifact_id` hors conv → `ATTACHMENT_NOT_IN_SCOPE` (C18).

## 4. Pièges

- Ne jamais écrire dans `Thot v0.8/` — backup intouchable, Phase 1 standalone.
- `data/temp/staged` = volatil (peut être purgé) ; seul `data/blobs` est durable.
- `image_paths` legacy → promotion hash→blob avec log `compat`, puis `artifact_id`.
