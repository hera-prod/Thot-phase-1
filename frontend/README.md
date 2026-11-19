# Frontend — React chat + compositeur + drag&drop (IPC Electron)

> Note : les renvois `docs/...`, `q-r.md` et à la « Bible » désignent la documentation interne du projet ; elle n'est pas incluse dans l'export HuggingFace.
>
> Le lancement via Electron requiert Node.js >= 22.12 (contrainte Electron verrouillee).

## 1. Dossiers englobés

```
frontend/
├── index.html                             ← coque HTML (aucune police distante : Manrope locale)
├── public/
│   ├── fonts/                             ← Manrope embarquée : manrope-{400,500,600,700}.woff2 + OFL.txt
│   └── assets/                            ← brand/ (Thot_logo.svg, Thot_logo_fond-blanc.png, Thot_filigrane.svg)
│                                            + empty-states/empty-history.png
├── src/main.tsx                           ← entrée Vite (port 1420 strictPort)
├── src/App.tsx                            ← AttachmentDraftProvider + routes (GlobalDragGuard, aujourd'hui sans effet)
├── src/lib/ipc.ts                         ← invoke/listen bas niveau + pathForFile (pont renderer → main Electron)
├── src/lib/ipcBridge.ts                   ← wrappers typés par verbe (chatSend, staging, attachmentsPrepare,
│                                            listConversations, listMessages, getAttachmentBytes, imageGenerate, …)
├── src/types/ipc.ts                       ← événements : backend_ready/thinking/tool_call/answer/completed/error
├── src/features/attachments/
│   ├── AttachmentDraftContext.tsx         ← brouillon stageItemId/stagedPath/previewToken (survit Home→Conv)
│   ├── attachmentTypes.ts
│   └── attachmentValidation.ts            ← 20 Mo max + WARN 5 Mo → resize 1536 (NaFlex côté backend)
├── src/features/inference/InferenceActivityContext.tsx
├── src/features/models/modelCatalog.ts
├── src/components/composer/
│   ├── Composer.tsx                       ← machine à états drag + envoi (halos, refus rouge, aria-live)
│   └── Composer.css
├── src/components/shell/                  ← AppShell, Sidebar, TitleBar
├── src/components/ui/                     ← Button, Toast, Tag, SearchInput
├── src/components/chat/                   ← Markdown (+ codePlugin.ts, markdown.css), OrchestratorTrace,
│                                            ToolCallDisplay, webSources.ts
├── src/components/espaces/                ← DeplacerDiscussionDialog
├── src/components/settings/               ← DepotRepairModal, ModelesSection (+ css), DepotIntegritySection (+ css)
├── src/components/image/                  ← GeneratedImageCard, ProgressCard, image.css
├── src/pages/chat/                        ← ChatHome.tsx (+ css), ChatConversation.tsx (+ css)
├── src/pages/history/HistoryPage.tsx
├── src/pages/gallery/GalleryPage.tsx (+ css)
├── src/pages/images/                      ← ImagesPage.tsx (+ css) + imageEditor/imageMask/imageGeometry/
│                                            imageEncoding/imageRequest/imageSnap (.ts)
├── src/pages/espaces/                     ← EspacesPage.tsx, EspacePage.tsx, format.ts
├── src/pages/settings/SettingsPage.tsx (+ css)
└── src/styles/                            ← global.css, tailwind.css, tokens.css
```

Appelés UNIQUEMENT via les verbes IPC d'Electron.
Le renderer ne connaît ni Electron ni le protocole du sidecar : il connaît des verbes nommés.

## 2. Pseudo-code (comment c'est fait)

```
# Boot
main.tsx → App.tsx :
  AttachmentDraftProvider (brouillon global : stageItemId, previewToken, needsResize)
  routes : ChatHome (nouveau) / ChatConversation (fil) / HistoryPage / Espaces / Gallery / Images / Settings

# Entrée fichier — pipeline UNIFIÉ (drag = trombone = collage)
drag HTML5 natif (écouteurs window + machine d'états du Composer) :
  window dragenter/dragover/dragleave/drop (preventDefault, throttle 120 ms sur dragover)
  Composer machine : repos → glisser_detecte → ciblage/hors_cible → dépôt/refus
    voile discret (halo, bordure, échelle, refus rouge + cause), aria-live "Dépôt possible"/"Fichiers reçus"
    pré-validation en vol : >4 fichiers, type hors PNG/JPG/WEBP/PDF, >20 Mo → refus
  → stageDropped : pathForFile(File) → chemins réels (résolus par le preload Electron)
  → stage_dropped_paths({request_id, paths}) → {items:[{stageItemId, sha256, preview:{token}}]}
  → File réhydraté via get_staged_preview_bytes (vrais bytes, mime détecté) → brouillon
  → thot:cmd attachments_prepare → artifact_ids [art_...]

trombone / collage :
  → stage_picker_bytes({request_id, filename, bytes}) → MÊME StagingResponse + MÊME AttachmentPreview

# Envoi message (via ipcBridge.chatSend → invoke("chat_send") → electron/main.cjs thot:cmd)
SUR envoyer :
  SI brouillon non vide :
    attachmentsPrepare(conversation_id, stage_item_ids) → artifact_ids [art_...]
  chatSend({request_id: uuid, message, attachment_ids:[art_...], conv_id, workspace_id})
  état ChatState : idle → preparing → orchestrating → tool_running → generating → idle
  events écoutés (listen) : thinking → tool_call/tool_result → answer (stream) → message_completed/error
  debugMode (thot_debug) affiche execution_plan {intent, selected_tool, selected_model, reason}

# Historique / rechargement (trace orchestrateur)
list_conversations(limit, offset) → résumés (servis par le sidecar Python, via IPC Electron)
list_messages(conv_id) → messages + attachments[] (ids seuls) + toolcalls[] + brief/plan/orchestrator_thinking
POUR chaque attachment sans blob local :
  bytes = getAttachmentBytes(artifact_id) → objectURL → vignette reconstruite
  SI pdf → chip 📄 (pas de pari asset://)
Bulle 🔍 Orchestrateur reconstruite à l'identique au reload (plan + outils + brief) ;
  sans trace (legacy), elle reste cachée — jamais de bulle vide.

# IPC Electron réellement utilisé (electron/main.cjs + electron/commands.cjs)
Renderer ──invoke("verbe", args)──▶ ipc.ts ──▶ preload.cjs ──▶ ipcRenderer.invoke
                                     ◀──── listen("nom", cb) ◄────── thot:event (main → renderer)
Main process (electron/main.cjs) :
  ipcMain.handle('thot:cmd', (command, args) → valide contre ALLOWED → sidecar.write → awaitReply)
  ipcMain.handle('thot:send', (command, payload) → sidecar.write)
  app:// protocole privilégié pour servir frontend/dist (SPA, pas de port Vite en prod)
  Protocole sidecar Python stdin/stdout JSON Lines (request_id de corrélation)
```

## 2bis. Schémas ASCII

```
ARBRE COMPOSANTS (qui englobe quoi)
───────────────────────────────────
App.tsx
 ├─ AttachmentDraftProvider  (brouillon global : stageItemId, previewToken, needsResize)
 └─ Routes
     ├─ ChatHome              (nouveau fil : composer + tray vides)
     ├─ ChatConversation      (fil : messages + tray + ChatState + execution_plan)
     │    ├─ Composer         (machine drag + envoi + halos + aria-live)
     │    └─ …                (OrchestratorTrace, ToolCallDisplay en debugMode)
     ├─ HistoryPage           (liste conversations via list_conversations)
     ├─ Espaces / Gallery / Images / Settings
```

```
IPC RENDERER → SIDECAR (Electron)
────────────────────────────────────────────
Renderer (ipcBridge.ts)
  │ invoke("chat_send", {requestId, message, attachmentIds, convId, workspaceId})
  │ invoke("list_conversations", {limit, offset})
  │ invoke("list_messages", {conversationId})
  │ invoke("get_attachment_bytes", {artifactId})
  │ invoke("image_generate", {requestId, prompt, …})
  │ invoke("stage_dropped_paths", {requestId, paths})
  │ invoke("stage_picker_bytes", {requestId, filename, bytes})
  │ invoke("attachments_prepare", {conversationId, stageItemIds, requestId})
  ▼
ipc.ts → preload.cjs → ipcRenderer.invoke("thot:cmd", command, args)
  ▼
electron/main.cjs : ipcMain.handle('thot:cmd')
  │ valide command dans ALLOWED (commands.cjs)
  │ sidecar.write(requestId, method, params)   → Python sidecar (stdin JSON Lines)
  │ awaitReply(requestId, event, method)       ← Python sidecar (stdout JSON Lines)
  ▼
Renderer reçoit le payload débarrassé (RESULT_KEY) → callback
```

```
ÉVÉNEMENTS PYTHON → RENDERER (listen)
─────────────────────────────────────
backend_ready → message_started → thinking → tool_call/tool_result → answer(stream)
  → message_completed / error
run_started → run_stage → run_completed / run_failed / run_cancelled / run_cancelling
artifact_created → backend_terminated
memory_started → memory_deferred → memory_finished
web_settings
artifact_ingestion_started → artifact_ingestion_finished → artifact_ingestion_failed
```

## 3. Extraits réels

```ts
// src/lib/ipcBridge.ts
stageDroppedPaths(requestId, paths) → StagingResponse
stagePickerBytes(requestId, filename, bytes) → StagingResponse
attachmentsPrepare(conversationId, stageItemIds, requestId) → { items: [{stageItemId, artifactId, status}] }
chatSend({ request_id, message, attachment_ids, conv_id, workspace_id })
listConversations(limit, offset) → ConversationSummary[]
listMessages(conversationId) → messages + attachments[] + toolcalls[] + brief/plan
getAttachmentBytes(artifactId) → [bytes, mime]
imageGenerate({ request_id, prompt, aspect_ratio, … }) → void
```

```ts
// electron/main.cjs — surface IPC minimale et vérifiée
ipcMain.handle('thot:cmd', async (_event, command, args) => {
  if (!ALLOWED.has(command)) throw new Error(`Commande refusée : ${command}`)
  const method = SIDECAR_METHODS[command]
  await sidecar.write(requestId, method, params)
  return await sidecar.awaitReply(requestId, replyEventFor(method), method)
})
```

## 4. Pièges

- `list_conversations` est servi par le sidecar Python via IPC invoke (`thot:cmd`) : le renderer n'appelle jamais le backend directement, ni une couche native de plateforme.
- Le drag & drop repose sur les événements HTML5 (écouteurs `window` + machine d'états du `Composer`), pas sur une API de dépôt natif : `onDragDropEvent` et `dragDropEnabled` n'existent pas ici. Les chemins réels sont résolus à la goutte par `pathForFile` (exposé par le preload Electron), puis validés côté backend (magic bytes).
- Police Manrope servie localement depuis `public/fonts/` (4 fichiers woff2 + licence OFL jointe) : `index.html` ne référence aucune URL distante, le premier rendu ne fait donc aucun appel réseau.
- `preview ?` fixé : même `AttachmentPreview` pour drag ET trombone (pas 2 composants).
- `needsResize` : image > WARN 5 Mo → resize 1536 tracé NaFlex (plus de rejet dur 5 Mo) ; seul > 20 Mo refuse.
- `composerShellRefus` rouge + libellé cause (`>4` / `type` / `>20 Mo`).
- Le dépôt unifié passe par `stage_dropped_paths` / `stage_picker_bytes` + `attachments_prepare` : un seul pipeline, une seule ingestion — en garder deux créait des doubles ingestions silencieuses.
