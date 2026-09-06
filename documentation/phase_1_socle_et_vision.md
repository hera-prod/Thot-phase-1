# Phase 1 — Socle, chat agentique et vision

> **Objectif** : poser le **squelette de bout en bout** sur lequel toutes les phases suivantes s'appuieront. Un chat fonctionnel avec une boucle agentique à deux couches, de la vision et de la compaction de contexte, le tout sous 8 Go de VRAM.

---

## 1. Périmètre fonctionnel

- **Accueil** : saisie du premier message, qui crée une nouvelle discussion et bascule vers la page de chat.
- **Historique** : liste des conversations précédentes, reprise au clic.
- **Page de chat** : envoi de messages, affichage du streaming, pièces jointes (image / PDF).
- **Boucle agentique** : un modèle, deux rôles (orchestrateur → générateur).
- **Outils (tool calling)** : vision générale, OCR de document, liste des sources, lecture de texte.
- **Compaction de contexte** : résumé automatique quand le contexte approche sa limite.

---

## 2. Architecture générale

```
┌──────────────────────────── Tauri (Rust) ────────────────────────────┐
│  Frontend React (Vite)      │   Sidecar manager (stdin/stdout)      │
│  - Accueil / Historique     │   - spawn du binaire Python            │
│  - Chat / Composer          │   - window.emit ↔ pipeline IPC         │
└──────────────┬──────────────┴──────────────▲────────────────────────┘
               │ messages JSON Lines         │ événements JSON Lines
               ▼                             │
        ┌────────────────────────────────────┴─────────────────────────┐
        │               Backend Python (sidecar PyInstaller)           │
        │  main ──► agent_loop ──► ModelManager ──► llama-cpp-python    │
        │              │              │                                 │
        │              ▼              ▼                                 │
        │        ToolRegistry    (swap séquentiel des modèles)          │
        │              │                                                │
        │              ▼                                                │
        │        SQLite (SQLAlchemy) + répertoire de fichiers            │
        └──────────────────────────────────────────────────────────────┘
```

Références :
- [Pseudo-code backend — socle (IPC, main, ModelManager)](../pseudocode/backend_socle.md)
- [Pseudo-code backend — boucle agentique](../pseudocode/backend_agent_loop.md)
- [Pseudo-code backend — outils](../pseudocode/backend_outils.md)
- [Pseudo-code backend — base de données](../pseudocode/backend_db.md)
- [Annexe 01 — LFM et attention linéaire](../annexes/01_lfm_et_architecture.md)
- [Annexe 02 — Quantification GGUF](../annexes/02_quantification_gguf.md)

---

## 3. Le bus IPC (JSON Lines)

Tauri et Python communiquent par **flux standard** (stdin/stdout), chaque message étant une ligne JSON séparée. Ce choix :
- évite d'ouvrir un port réseau local (plus simple, plus sûr) ;
- se package naturellement dans un binaire `PyInstaller` géré comme *sidecar* ;
- impose une règle stricte : **un objet JSON = une ligne**, sans retour à la ligne interne.

Événements émis par le backend (phase 1) :

| Événement | Rôle |
| :--- | :--- |
| `backend_ready` | informant l'UI que le sidecar est prêt (avec backend CUDA/Vulkan/CPU) |
| `thinking` | raisonnement interne de l'orchestrateur (non montré par défaut) |
| `tool_call` | un outil est invoqué |
| `tool_result` | résultat d'un outil |
| `answer` | flux de la réponse générée |
| `message_completed` | fin du tour |
| `error` | erreur remontée |

> **Note importante** : la couche « thinking » n'est **jamais** réinjectée dans l'historique visible ni en base. Seule la réponse finale (`answer`) est persistée pour l'utilisateur. Voir [pseudo-code — boucle agentique](../pseudocode/backend_agent_loop.md).

---

## 4. La boucle agentique (orchestrateur → générateur)

Un seul et même modèle (LFM 2.5 8B) assume deux rôles distincts via **deux prompts système différents** :

1. **Orchestrateur** — lit la requête, décide s'il faut un outil, prépare un *brief* et les résultats.
   - Il **peut** appeler les outils (`vision`, `doc_ocr`, `get_source`, `read_text`).
   - Limité à un nombre maximal d'itérations (ex. 5) et un délai par appel.
2. **Générateur** — reçoit le *brief* + les résultats préparés, produit la réponse finale.
   - Il ne possède **aucun** outil. C'est une séparation stricte pour éviter les boucles parasites.

> **Pourquoi ce découpage ?** Injecter les traces de raisonnement et les appels d'outils dans le contexte du générateur pollue la conversation et fait déraper le modèle. En isolant les deux rôles, on garde un historique propre (`user ↔ assistant` uniquement) tout en conservant la capacité d'agir.

---

## 5. Gestion mémoire (swap séquentiel)

Voir l'annexe sur [la quantification GGUF](../annexes/02_quantification_gguf.md) pour les tailles exactes. Le principe central :

- **Un seul modèle résident à la fois** en VRAM. `ModelManager` est le gardien unique.
- Le LLM principal (8B) reste **chaud** entre deux requêtes (pas de rechargement systématique).
- Les modèles de vision (VL-3B, GLM-OCR) sont chargés **paresseusement** uniquement lors d'un `tool_call`, puis **déchargés explicitement** (close + suppression + libération).

Le contexte adaptatif (`resolve_n_ctx`) choisit 128k si la VRAM le permet, sinon un repli (ex. 32k), ce qui évite les dépassements mémoire (OOM).

référence : [Annexe 01 — LFM et attention linéaire](../annexes/01_lfm_et_architecture.md) pour comprendre pourquoi 128k de contexte reste tenable ici.

---

## 6. Les outils de vision

Trois capacités visuelles coexistent, servies par **deux modèles** :

| Outil | Modèle | Usage |
| :--- | :--- | :--- |
| `vision` | LFM2.5-VL-3B (quantifié) | question générale sur une image (« qu'est-ce qu'il y a ? ») |
| `doc_ocr` | GLM-OCR | lecture de texte, tableaux, PDF, factures, scans |
| `get_source` / `read_text` | (pas de modèle) | lister les fichiers de la discussion / lire un texte joint |

> **Routage PDF vs image** : un PDF joint est d'abord aiguillé vers `doc_ocr` ; une image vers `vision`. Une demande explicite « lis le texte » bascule vers l'OCR.

---

## 7. Compaction de contexte

Quand le contexte approche ~110 000 jetons (sur 128k), le même modèle est appelé avec un **prompt de compaction** pour produire un résumé qui devient le nouveau contexte de la discussion.

- La compaction **préserve les faits et les numéros**, pas le verbatim.
- Le résultat remplace l'historique détaillé pour les tours suivants.
- Les générations passées ne réinjectent **pas** les traces des orchestrateurs : seul le dialogue visible est conservé.

référence : [Pseudo-code backend — socle/compaction](../pseudocode/backend_agent_loop.md).

---

## 8. Stockage

Base SQLite (SQLAlchemy), 4 tables initiales : `conversations`, `messages`, `toolcalls`, `sources`. Les fichiers (images, PDF) sont stockés **sur disque** dans un répertoire centralisé et référencés par identifiant/hash — jamais en base64 dans la base ni dans l'IPC.

> **Anticipation des phases suivantes** : le schéma est conçu pour être étendu sans refonte (voir [phase 5](phase_5_espaces_et_memoire.md)). Ajouter une colonne `media_kind`, `sha256`, `relative_path` dès maintenant évite de migrer plus tard.

---

## 9. Points de vigilance (ne pas confondre)

- **`thinking` ≠ réponse** : ne jamais mélanger le raisonnement et la réponse finale dans l'historique.
- **« vision » (généraliste) ≠ « doc_ocr » (texte)** : deux modèles distincts, deux objectifs différents.
- **Un seul modèle en VRAM** : ne jamais charger deux modèles simultanément.
- **Chemin de fichier halluciné** : valider qu'un chemin demandé par le modèle correspond bien à un fichier réellement fourni avant de l'utiliser.