# Pseudo-code — Frontend (Tauri + React)

> Phases 1 → 5. Ce fichier couvre la **navigation**, les **pages**, le **bus IPC côté TypeScript** et les **composants**, avec l'évolution par phase.

---

## 1. IPC côté frontend

```ts
[Phase 1]
// lib/tauri.ts — pont vers le sidecar Python via l'API Tauri.
// NOTE : le transport est JSON Lines (stdin/stdout), géré côté Rust (sidecar manager).
function invoke(method: string, params: object) { ... }        // requête → réponse
function listen(event: string, cb: (payload) => void) { ... }  // abonnement aux événements

// ⚠️ événements (backend → UI) à écouter en phase 1 :
// backend_ready, thinking, tool_call, tool_result, answer, message_completed, error
```

---

## 2. Structure de navigation (AppShell / Sidebar)

```tsx
[Phase 1]
// AppShell : layout global + barre latérale.
// Routes phase 1 : Accueil (ChatHome), Historique (HistoryPage), Chat (ChatConversation).
function AppShell() {
  return (
    <Sidebar items={["Accueil", "Historique"]} />      // [Phase 2] + "Génération", "Galerie"
                                                        // [Phase 5] + espaces
    <Outlet />                                          // page active
  )
}
```

> **Anticipation** : dès la phase 1, définir la navigation par **onglets/route** (Accueil, Chat, Historique, Paramètres) pour pouvoir ajouter `/generate` et `/gallery` sans refonte en phase 2.

---

## 3. Page de chat

```tsx
[Phase 1]
function ChatConversation({ conversation }) {
  // - Composer : champ de saisie + pièces jointes (drop d'image/PDF)
  // - AssistantTurn : affiche la réponse finale (⚠️ PAS le "thinking")
  // - ToolCallDisplay : affiche l'état des outils appelés (en attente / résultat)
  //
  // ⚠️ le front "strip" aussi les fuites de thinking, mais la VÉRITÉ est dans le backend
  //    (qui ne persiste jamais le thinking dans l'historique).
}
```

---

## 4. Pièces jointes

```tsx
[Phase 1]
// features/attachments : validation des types + prévisualisation.
// NOTE : on envoie des IDENTIFIANTS/chemins au backend, jamais le contenu binaire
//       en base64 dans les messages (⚠️ saturation du bus sinon).
function useAttachmentDrop() { ... }
function attachmentValidation(file) { ... }   // vérifie type/size avant envoi
```

---

## 5. Évolutions par phase

### [Phase 2] Génération et galerie

```tsx
[Phase 2]
// Route /generate : formulaire prompt + références + aspect ratio + jauge de progression.
// Route /gallery  : liste des images (grille + vignettes), filtre par catégorie.
function GalleryPage() {
  // ⚠️ la galerie interroge la MÊME base que l'historique, filtrée par catégorie.
  // [Phase 3] cette vue accueille aussi PDF / CSV / Markdown.
}
```

### [Phase 3] Audio

```tsx
[Phase 3]
// Bouton micro : capture l'audio, envoie en tampon au thread STT, injecte le texte.
// Bouton lecture : appelle le TTS, joue le flux audio renvoyé.
// NOTE : la transcription est un PÉRIPHÉRIQUE (thread dédié), pas un appel d'outil.
```

### [Phase 5] Espaces

```tsx
[Phase 5]
// Sidebar : sélecteur d'espace + liste des discussions de l'espace.
// Page espace : dossier utilisateur (lecture seule) + aperçu de la mémoire.
```

---

## Points de vigilance

- **Ne pas afficher le `thinking`** : seule la réponse finale est visible.
- **Identifiants, pas base64** : le front passe des références, le backend résout les fichiers.
- **La galerie ≠ l'historique** : deux vues distinctes (l'une liste les fichiers, l'autre les conversations).
- **Audio (phase 3) ≠ outil** : l'entrée/sortie vocale ne passe pas par le callable tools du LLM.

---

## Correspondance

- [Document — Phase 1](../phase_1_socle_et_vision.md) · [Phase 2](../phase_2_generation_image.md) · [Phase 5](../phase_5_espaces_et_memoire.md)
- Avant : [backend_socle.md](backend_socle.md)