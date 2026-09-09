# Pont Rust — Tauri IPC + sidecar + portable

## 1. Dossiers englobés

```
src-tauri/
├── src/lib.rs             ← TOUT le pont : resolve_*, sidecar spawn, staging, chat_send, events
├── src/main.rs            ← entrée binaire (délègue à lib.rs)
├── tauri.conf.json        ← dragDropEnabled:true, csp restrictive, externalBin thot-sidecar
├── capabilities/default.json ← shell:allow-execute (spawn sidecar autorisé)
└── binaries/              ← thot-sidecar-x86_64-unknown-linux-gnu (PyInstaller, git-ignoré en dev)
```

Frontend associé : `frontend/src/lib/tauri.ts` (wrappers `invoke`/`listen` typés).

## 2. Pseudo-code (comment c'est fait)

```
# Démarrage (setup)
data_dir = resolve_data_dir() :
  SI portable.json trouvé (exe / resource_dir / parents, jamais CWD) → <racine>/data
  SINON → XDG app_data_dir()/data
  créer data/{database, blobs, previews, derived, temp, trash, logs}
db_path = data/database/thot-phase-1.db
spawn sidecar avec env THOT_DATA_DIR / THOT_DB_PATH / THOT_APP_DATA_DIR (source vérité)
  sidecar = CommandChild(stdin=JSON Lines, stdout=JSON Lines)
  stdout_ligne → window.emit(event, payload)  # Rust ne parse pas le métier, il relaie

# Staging (entrée UI → Rust seul, SANS Python)
stage_dropped_paths({request_id, paths}) :   # drag natif (fichiers déjà sur disque)
  POUR chaque path :
    valider : refus si >4 au total / dossier / .lnk / type inconnu / >20 Mo
    lire bytes → sha256 → écrire data/temp/staged/{hash}.ext
    preview = {token} pour convertFileSrc / objectURL
  retourner {stageId, items:[{stageItemId: sti_..., sha256, mimeDetected, preview}], rejected}

stage_picker_bytes({request_id, filename, bytes}) :  # trombone / collage (bytes en mémoire)
  MÊME StagingResponse que ci-dessus (pipeline unifié Lot 2)

get_staged_preview_bytes(stageItemId) → bytes pour URL.createObjectURL (même AttachmentPreview drag/trombone)

attachments_prepare({conversation_id, stage_item_ids}) :
  POUR chaque sti_... : copier staged → data/blobs/sha256/ab/...bin + créer Artifact/ArtifactLink
  retourner [{stageItemId, artifactId: art_..., status}]

# Chat (relai pur, pas de logique métier)
chat_send({requestId, message, mode, attachmentIds, convId}) :
  écrire UNE ligne JSON sur stdin sidecar : {id, method:"chat.send", params:{...}}
  # les events reviennent sur stdout → window.emit → frontend on*

# Historique rapide (sans Python)
list_conversations → rusqlite direct sur thot-phase-1.db (plus de `python3 -c`)
```

## 2bis. Schémas ASCII

```
RÉSOLUTION PORTABLE (jamais CWD — toujours lanceur/ressources)
──────────────────────────────────────────────────────────────
current_exe()/parent ─┐
                      ├─▶ find_portable_root() ── portable.json trouvé ? ──OUI──▶ <racine>/data
resource_dir (Tauri) ─┘      (remonte 2 niveaux)                        NON
                                                                    ▼
                                                        XDG app_data_dir()/data

  puis create_dir_all : database/ blobs/ previews/ derived/ temp/ trash/ logs/
  env sidecar : THOT_DATA_DIR / THOT_DB_PATH / THOT_APP_DATA_DIR (source vérité)
```

```
PONT STDIN/STDOUT (Rust = relai pur, 0 logique métier)
─────────────────────────────────────────────────────
FRONTEND                    RUST (lib.rs)                    PYTHON (sidecar)
   │                            │                                   │
   │ invoke("chat_send",        │                                   │
   │  {requestId, message,      │                                   │
   │   attachmentIds, convId})  │                                   │
   │ ──────────────────────────▶│── stdin JSONL ───────────────────▶│
   │                            │ {id, method:"chat.send", params}  │ loop.run_chat()
   │                            │                                   │   │
   │ listen("answer"...)        │◀─ stdout JSONL ───────────────────│───┘
   │ ◀──────────────────────────│ {event, payload, proto:1,         │ emit(run_id, seq++)
   │  window.emit(event,payload)│  run_id, seq}                     │
   │                            │                                   │
   │ STAGING (sans Python) :    │                                   │
   │  stage_dropped_paths ─────▶│── data/temp/staged/{hash}.ext     │
   │  stage_picker_bytes ──────▶│   {stageId, sti_..., sha256,      │
   │  attachments_prepare ─────▶│    preview:{token}, rejected}     │
   │                            │── blobs + Artifact (via Python)   │
   │  list_conversations ──────▶│── rusqlite direct (sans Python)   │
```

## 3. Extraits réels

```rust
// src-tauri/src/lib.rs — jamais CWD, toujours lanceur/ressources
fn find_portable_root(start: &Path) -> Option<PathBuf> // remonte 2 niveaux, cherche portable.json
fn resolve_data_dir(app) // portable → <racine>/data sinon XDG + create_dir_all(database, blobs, logs, temp, backups)
```

```ts
// frontend/src/lib/tauri.ts — snake_case Rust -> camelCase JS
chatSend({ request_id, message, mode, attachment_ids, conv_id })
invoke("chat_send", { requestId, message, mode, attachmentIds, convId })
```

## 4. Pièges

- Tauri mappe `request_id` → `requestId` : oublier le mapping = `invalid args requestId missing`.
- HTML5 drag inactif sous Tauri → natif pur `onDragDropEvent` + `GlobalDragGuard` (pas de `stopPropagation`).
- `lastNativeDropRef 250ms` + dedup `nom` évitent le triple dépôt (window + container + natif).
