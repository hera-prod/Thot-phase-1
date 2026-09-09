# Frontend — React chat + composer + drag&drop

## 1. Dossiers englobés

```
frontend/
├── src/main.tsx                          ← entrée Vite (port 1420 strictPort)
├── src/App.tsx                           ← AttachmentDraftProvider + GlobalDragGuard + routes
├── src/lib/tauri.ts                      ← wrappers invoke/listen typés (chatSend, staging, prepare)
├── src/types/ipc.ts                      ← events : backend_ready/thinking/tool_call/answer/completed/error
├── src/features/attachments/
│   ├── AttachmentDraftContext.tsx        ← brouillon stageItemId/stagedPath/previewToken (survit Home→Conv)
│   ├── AttachmentTray.tsx / attachmentTypes.ts / attachmentValidation.ts (20 Mo + WARN 5 Mo → resize 1536)
│   └── useAttachmentDrop.ts              ← stub natif (vrai drop = onDragDropEvent fenêtre)
├── src/components/composer/Composer.tsx  ← machine à états drag + envoi (halos, refus rouge, aria-live)
├── src/components/chat/AssistantTurn.tsx + ToolCallDisplay.tsx
├── src/components/shell/ + ui/           ← AppShell, Sidebar, TitleBar, Button/Toast/Tag/SearchInput
├── src/pages/chat/ChatHome.tsx + ChatConversation.tsx ← ChatState + historique + execution_plan
└── src/pages/history/HistoryPage.tsx
```

## 2. Pseudo-code (comment c'est fait)

```
# Boot
main.tsx → App.tsx :
  AttachmentDraftProvider (brouillon global : stageItemId, previewToken, needsResize)
  GlobalDragGuard (preventDefault SANS stopPropagation — laisse le natif passer)
  routes : ChatHome (nouveau) / ChatConversation (fil) / HistoryPage

# Entrée fichier — pipeline UNIFIÉ (drag = trombone = collage, Lot 2)
drag natif (fenêtre onDragDropEvent, dragDropEnabled:true) :
  Composer machine : repos → détecté → ciblé/hors_cible → dépôt/annulé (+ refus si >4/dossier/.lnk/type)
    voile discret (glisser_detecte, hors_cible, ciblé bordure/halo/échelle, refus rouge)
    prefers-reduced-motion → bordure statique (T-ATT-19), aria-live "Dépôt possible"/"Fichiers reçus"
  → stage_dropped_paths({request_id, paths}) → {items:[{stageItemId, sha256, preview:{token}}]}
  → File placeholder + vrais bytes via get_staged_preview_bytes → URL.createObjectURL
  → thot:add-files → addFiles (dedup nom, lastNativeDropRef 250ms anti-triple dépôt)

trombone / collage :
  → stage_picker_bytes({request_id, filename, bytes}) → MÊME StagingResponse + MÊME AttachmentPreview
  → T-ATT-01 : trombone = drag, même vignette

# Envoi message
SUR envoyer :
  SI brouillon non vide :
    {items} = attachmentsPrepare(conversation_id, stage_item_ids) → artifact_ids [art_...]
  chatSend({request_id: uuid, message, mode, attachment_ids:[art_...], conv_id})
  état ChatState : idle → preparing → orchestrating → tool_running → generating → idle
  events écoutés : thinking → tool_call/tool_result → answer (stream) → message_completed/error
  debugMode (thot_debug) affiche execution_plan {intent, selected_tool, selected_model, reason}

# Historique / rechargement (09/09)
list_messages(conv_id) → messages + attachments[] (ids seuls, artefacts + legacy)
POUR chaque attachment sans blob local :
  bytes = get_attachment_bytes(artifact_id) → objectURL → vignette reconstruite
  SI pdf → chip 📄 (pas de pari asset://)
```

## 2bis. Schémas ASCII

```
ARBRE COMPOSANTS (qui englobe quoi)
───────────────────────────────────
App.tsx
 ├─ AttachmentDraftProvider  (brouillon global : stageItemId, previewToken, needsResize)
 ├─ GlobalDragGuard           (preventDefault SANS stopPropagation)
 └─ Routes
     ├─ ChatHome              (nouveau fil : composer + tray vides)
     ├─ ChatConversation      (fil : messages + tray + ChatState + execution_plan)
     │    ├─ Composer         (machine drag + envoi + halos + aria-live)
     │    ├─ AttachmentTray   (vignettes objectURL + chips 📄 PDF)
     │    ├─ AssistantTurn    (rendu réponse + thinking)
     │    └─ ToolCallDisplay  (tool_call/result en debugMode thot_debug)
     └─ HistoryPage           (liste conversations via list_conversations Rust)
```

```
MACHINE DRAG COMPOSER (annexe 09/10)
────────────────────────────────────
                    hover fenêtre (Files ?)
  ┌───────┐  ─────────────────────────▶  ┌──────────┐
  │ repos │                              │ détecté  │  (voile discret)
  └───┬───┘  ◀─────────────────────────  └────┬─────┘
      │         quitte / drop annulé           │
      │                                        ▼
      │              ┌─────────────┐    dedans  ┌────────┐
      │              │ hors_cible  │ ◀───────── │ ciblé  │ (bordure/halo/échelle)
      │              └─────────────┘            └───┬────┘
      │                                            │ drop
      ▼                                            ▼
┌───────────┐  >4/dossier/.lnk/type   ┌──────────────────┐
│  refus    │ ◀────────────────────── │ dépôt → staging  │
│ (rouge +  │                         │ sti_... + token  │
│  libellé) │                         └──────────────────┘
└───────────┘
  prefers-reduced-motion → bordure statique · aria-live "Dépôt possible"/"Fichiers reçus"
  anti-triple : dedup nom + lastNativeDropRef 250ms (window+container+natif)
```

```
ENVOI + HISTORIQUE
──────────────────
[SUR envoyer]
  brouillon ? ──OUI──▶ attachmentsPrepare(sti_...) → [art_...]
  chatSend({uuid, message, mode, attachment_ids, conv_id})
  ChatState : idle → preparing → orchestrating → tool_running → generating → idle
  events    : thinking → tool_call/tool_result → answer(stream) → completed/error

[RECHARGEMENT 09/09]
  list_messages → messages + attachments[] (ids seuls)
    └─ POUR chaque attachment : get_attachment_bytes → objectURL → vignette
       SI pdf → chip 📄 (jamais de pari asset://)
```

## 3. Extraits réels

```ts
// src/lib/tauri.ts
stageDroppedPaths(requestId, paths) → StagingResponse
stagePickerBytes(requestId, filename, bytes) → StagingResponse  // même forme
attachmentsPrepare(conversationId, stageItemIds, requestId) → [{stageItemId, artifactId}]
chatSend({ request_id, message, mode, attachment_ids, conv_id })
```

```ts
// Composer — détection sans casser le natif
isDraggingGlobal + isDraggingOverComposer (dragCounterRef + dataTransfer.types Files + isInsideComposer)
// natif : position/scaleFactor + validateHover pré-validation
MAX_ATTACHMENTS = 4
```

## 4. Pièges

- `preview ?` fixé : même `AttachmentPreview` pour drag ET trombone (pas 2 composants).
- `needsResize` : image > WARN 5 Mo → resize 1536 tracé NaFlex (plus de rejet dur 5 Mo).
- `composerShellRefus` rouge + libellé cause (`>4` / `dossier/.lnk` / `type`).
