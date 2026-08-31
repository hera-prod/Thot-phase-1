# Futur — Brouillon Phase 1 (Thot-phase-1)

## Périmètre

`Futur/` est un dossier de brouillon. Le code réel sera dans un **nouveau dossier `Thot-phase-1/`** entièrement indépendant, inspiré de ce projet mais **sans toucher au code existant**.

Ce sera un **brouillon de conception** de Thot v0.9. Un produit standalone complet, réécrit proprement, réutilisant uniquement les modèles et la base de données (SQLite, mais nouvelle DB).

> **Règle absolue** : On ne modifie **jamais** les fichiers de ce projet (`Thot v0.8/`). Tout le code de la Phase 1 est dans `Thot-phase-1/`.

### Arborescence `Thot-phase-1/` validée 30/08 (Réponses.md §1)

```
Thot-phase-1/
├── backend/
│   ├── inference/
│   │   ├── hardware.py   # HardwareDetector + estimate_layers()
│   │   ├── manager.py    # ModelManager séquentiel (supports_vision, resolve_n_ctx)
│   │   └── ipc.py        # emit()/log() + stdin loop JSON Lines
│   ├── prompts/
│   │   ├── orchestrator.py
│   │   ├── generator.py
│   │   └── compaction.py # résumé <800 tokens
│   ├── db/
│   │   ├── models.py     # SQLAlchemy 4 tables (CASCADE + INDEX)
│   │   └── database.py
│   ├── config/
│   │   └── defaults.py   # ModelConfig frozen
│   ├── tests/            # Phase 1 minimal
│   │   ├── test_hardware.py  # mock nvidia-smi
│   │   └── test_manager.py
│   └── main.py           # boot + warmup
├── src-tauri/
│   ├── binaries/         # thot-sidecar-<triple> (externalBin, pas resources/)
│   ├── src/main.rs       # sidecar spawn + BufReader::lines() → window.emit
│   ├── capabilities/default.json
│   └── tauri.conf.json   # bundle.externalBin
├── frontend/src/
│   ├── lib/tauri.ts      # invoke/listen typés + Zod + request_id
│   └── types/ipc.ts
├── models/
├── pyproject.toml        # racine, pip + pyinstaller (pas de uv)
├── requirements.txt      # pip install -r requirements.txt (pas de .venv)
└── README.md
```

---

## Architecture système

```
┌─────────────────────────────────┐
│  Application Desktop (Tauri)    │
│                                 │
│  ┌───────────────────────────┐  │
│  │  Frontend (React 19)      │  │
│  │  • Pages chat, agent      │  │
│  │  • UI streaming           │  │
│  └────────┬────────────────┘  │
│           │ IPC (Tauri channels)│
│  ┌────────▼────────────────┐  │
│  │  Backend (Rust Tauri)   │  │
│  │  • Gestion IPC          │  │
│  │  • Gestion sessions     │  │
│  └────────┬────────────────┘  │
│           │ (subprocess)       │
│  ┌────────▼────────────────┐  │
│  │  Backend (Python)       │  │
│  │  • llama-cpp-python     │  │
│  │  • Agent loop           │  │
│  │  • Tools                │  │
│  └─────────────────────────┘  │
└────────────────────────────────┘
```

**Flux :**
- Le **frontend React** communique avec le **backend Rust Tauri** via **IPC** (pas de REST)
- Le **backend Rust** lance le **backend Python** en subprocess
- Le **backend Python** charge les modèles via **llama-cpp-python**, exécute les tools, renvoie les résultats au Rust via stdin/stdout ou subprocess communication

### Choix : IPC vs REST pour Tauri

Dans Tauri, le frontend (React) et le backend (Rust) communiquent via **IPC**, pas HTTP. C'est plus rapide, plus simple, et plus natif. Le backend Python est un subprocess géré par le Rust.

Cela signifie qu'il n'y a **pas d'API REST** dans `Thot-phase-1` pour la communication frontend ↔ backend. Le frontend appelle des commandes Rust via `invoke()`, et Rust lance les scripts Python en subprocess.

> **Nota corrigée 30/08/2026 :** Pas de REST non plus entre Rust ↔ Python. Python est un subprocess pur : Rust écrit sur **stdin** (JSON) et lit **stdout en JSON Lines** ligne par ligne. Zéro HTTP partout.

---

## Ce qui est dans le scope (core Phase 1)

### 1. Chat + historique

- **Conversations** : créer, lister, modifier, supprimer, récupérer l'historique
- **Messages** : stocker messages, thinking, streaming
- **Outils (toolcalls)** : historique des appels d'outils (vision, OCR)
- **Sources** : sources des outils (si applicable)

### 2. Agent loop 2 couches (orchestrateur → brief → generator)

**Orchestrateur :**
- Lit le message utilisateur
- Décide : réponse directe (DIRECT_ANSWER) ou besoin d'outil ?
- **PEUT** appeler des outils (vision, OCR) pour collecter des informations
- Quand il a tout ce dont il a besoin → produit un brief pour le générateur
- **NE PEUT PAS** générer de réponse finale

**Générateur :**
- Reçoit uniquement le brief de l'orchestrateur
- **NE PEUT JAMAIS** appeler d'outils
- Génère la réponse en streaming (thinking + answer)
- Termine par `MessageCompleted`

**Règle absolue (validée Réponses.md §2):**
> Orchestrateur = YES tool calls | Générateur = NO tool calls — même si `consecutive_tool_errors>=3`, l'orchestrateur retry, jamais le generator
> 7 events uniquement: `message_started, thinking, tool_call, tool_result, answer, message_completed, error`
> Budgets Phase 1: `max_iterations=5`, `tool_timeout 15s`, `total 300s`

**Double historique — 2 discussions en 1 (Réponses.md §2 + §6, validé 30/08):**
- **Historique partagé** `messages(role=user|assistant)` = vu par les deux pour les tours passés. Orchestrateur **ne voit PAS** les `tool_call/tool_result` des tours précédents (bruit + divergence 128K évitée).
- **Tour en cours uniquement** : orchestrateur voit ses `tool_result` en mémoire, génère `brief` JSON (`json_schema` strict via `PromptConfig`, pas `gbnf` brut) → générateur reçoit `[historique partagé + brief + prepared_results]`.
- **Persistance** : `toolcalls` stockés pour debug (`visibility=ephemeral`) mais **jamais réinjectés** aux tours suivants. Si outil échoue → injecter signal minimal `{"role":"assistant","content":"[Outil doc_ocr a échoué: timeout]"}` pour éviter re-boucle.
- **Parité stricte** : `assert len(orch_history) == len(gen_history)` avant chaque `create_chat_completion`.
- **Compaction Opencode (Réponses.md §6)** : sur historique partagé uniquement. Seuils `PRUNE_PROTECT 40K / COMPACT 85% (109K/128K) / PRESERVE_RECENT 15K`. Étapes : `prune_old_tool_results()` (>40K), split `old|recent` (15K récents intouchables), résumé LLM `SYSTEM_COMPACTION` ("Ce qui accompli, travail en cours, fichiers, prochaines étapes...") `max_tokens 2048 temp 0.3` → nouvel historique `[ {role:"system", content:"[Résumé conversation]: ..."} ] + recent`. Appelé dans `get_orchestrator_context()` avant chaque tour, persisté `save_compacted_history()`. RAG Phase 2 si besoin OCR passé.

**Prompts figés — PromptConfig (Réponses.md §3 B filtré, garde notre contexte texte pur) :**
```python
@dataclass(frozen=True)
class PromptConfig: system: str; temperature: float; top_p: float; max_tokens: int; json_schema: dict|None

ORCHESTRATOR = PromptConfig(system=SYSTEM_ORCHESTRATOR, temperature=0.2, top_p=0.9, max_tokens=2048,
  json_schema={"type":"object","properties":{"mode":{"enum":["DIRECT_ANSWER","USE_FINDINGS"]},"brief":{"type":"string"},"tool_calls":{"type":"array","items":{"type":"object"}}}}, required=["mode","brief"])
GENERATOR = PromptConfig(system=SYSTEM_GENERATOR, temperature=0.2, top_p=0.9, max_tokens=8192, json_schema=None)
COMPACTION = PromptConfig(system=SYSTEM_COMPACTION, temperature=0.3, max_tokens=2048)
VISION = PromptConfig(system=SYSTEM_VISION, temperature=0.2, top_p=0.9, max_tokens=2048)
OCR = PromptConfig(system=SYSTEM_OCR, temperature=0.0, top_p=1.0, max_tokens=8192)
```
*Filtre : garde nos prompts spécifiques (texte pur, double historique), ajoute `json_schema` pour brief (évite parsing fragile) comme proposé, mais ignore le générique `React/Vue/Svelte` de l'outil recherche.*

### 3. Tools (2 outils, en mode agent) — contrat figé 30/08 (Réponses.md §1-4)

**ToolVision** : `LFM2.5-VL-3B` (32K, dense) → description
- Nom : `vision`
- Args : `{"query": "Décris ce graphique", "image_path": "/tmp/thot/{request_id}/{hash}.png", "image_hash": "sha256:..."}` — **hash+path, jamais base64** (base64 explose JSON/contexte)
- Prompt : `SYSTEM_VISION` 3-5 phrases factuelles, `temperature=0.2 / top_p 0.9 / max_tokens 2048`
- Résultat : `result: string` texte brut

**ToolDocOCR** : `GLM-OCR` (8K, Q4_K_M 0.6GB) → markdown fidèle
- Nom : `doc_ocr`
- Args : `{"query": "Extrais tableaux en markdown", "pdf_path": "/tmp/thot/{request_id}/doc.pdf", "image_hash": "sha256:..."}` — hash+path
- Prompt : `SYSTEM_OCR` fidélité absolue, structure markdown (`# | - | table`), `temperature=0.0 / top_p 1.0 / max_tokens 8192`, page par page PyMuPDF 200 DPI → `"\n\n--- Page suivante ---\n\n".join(results)`
- Résultat : markdown assemblé, max 50 pages

**Exécution (Réponses.md §3)** : **Séquentiel 1 tool_call / itération, max 5 iter**, `timeout 15s`, **retry 1x** avec backoff 1s, `latency_ms` mesuré `perf_counter`, sur échec `ok=false` + signal historique `{"role":"assistant","content":"[Outil doc_ocr a échoué: timeout]"}` pour éviter re-boucle. Pages OCR séquentielles (pas batch, évite KV blowup).

**Résultat → Générateur (Réponses.md §4)** : Persistance DB `result` complet pour audit, mais injection générateur **préparée** : `≤8K tokens → brut`, `8-16K → tronqué "[Tronqué ...]"`, `>16K → résumé LLM 500 mots (preserve faits/chiffres)` via `summarize_tool_result()`. Générateur reçoit `[brief + prepared_results]`.

Pas de toolset complexe. Modes : `chat` = pas d'outil (DIRECT_ANSWER) / `agent` = vision+doc_ocr

### 4. Base de données

- **Nouvelle DB SQLite** (`data/thot-phase-1.db`), indépendante de `data/thot.db`
- Tables : `conversations`, `messages`, `toolcalls`, `sources`
- Les autres tables (spaces, documents, etc.) silencieuses
- SQLAlchemy pour les ORM models

### 5. Modèles

- **llama-cpp-python** comme library de chargement (installable, ne nécessite pas de compilation C++)
- Tous les modèles chargés via `llama-cpp-python` (pas de llama-server, pas de CUDA dédié)

---

## Modèles assignés

### 1. LFM2.5-8B-A1B (Orchestrateur + Générateur)

**Fichier :** `models/LLM/LFM2.5-8B-AB-UD-Q4_K_M.gguf`
**Architecture :** Liquid AI — hybride MoE (18 x double-gated conv LIV + 6 x GQA), sparse MoE 32 experts top-4 sigmoid (cf LFM2.5-8B-A1B mai 2026)
**Rôle :** LLM principal pour chat + agent loop (reasoning-only, chain-of-thought avant answer)

| Paramètre | Valeur |
|-----------|--------|
| Paramètres total | 8.3B total / **1.5B actifs** (pas 1B, A1B = approx marketing) |
| Couches | 24 (18 conv LIV + 6 GQA) |
| Heads | 32 (hidden 2048, head 64) |
| Key-value heads | 8 (GQA) |
| Dimension cachée | 2048 |
| Dimension feed-forward | 7168 |
| Contexte max | **128K** (VL-3B = 32K, ne pas confondre) |
| Vocabulaire | 128 000 (doublé 65k→128k, 38T pretrain) |
| Quantization | Q4_K_M |
| Fraction d'actifs | ~18% (1.5B/8.3B) |
| Experts total | 32 |
| Top-k experts actifs | 4 (sigmoid gating) |
| Mémoire Q4_K_M | ~5.5 GB |
| Mémoire FP16 | ~16.5 GB |
| KV cache 128K Q8_0 | ~6.0 GB (32K ~1.5 GB) — cf 18432 B/tok q4_0 = 2.25GB 128K |
| Params reco | `temp 0.2 / top_k 80 / rep_pen 1.05` |
| **Capacité vision** | **❌ NON — texte pur uniquement** (pas d'encodeur) |
| Mode | Reasoning chain-of-thought obligatoire |

---

### 2. GLM-OCR (Tool doc_ocr)

**Fichier :** `models/Vision-document/GLM-OCR-Q4_K_M.gguf` **[validé 30/08/2026 — remplace f16]**
**Architecture :** GLM-V/GLM-4 Decoder + MTP (0.9B base `zai-org/GLM-OCR`, converti GGUF `ggml-org/mradermacher`)
**Rôle :** OCR technique pour documents PDF, tableaux, infographies

| Paramètre | Valeur |
|-----------|--------|
| Paramètres total | 0.9B (0.4B LLM + ~0.5B Vision) |
| Couches | 24 (+1 MTP) |
| Contexte outil | 8192 |
| Mémoire F16 | 1.9 GB (`overkill` selon mradermacher) |
| Mémoire Q4_K_M | **0.6 GB** (`fast, recommended`, préservé OmniDocBench) |
| Mémoire Q8_0 | 1.1 GB |
| mmproj | `mmproj-f16` 1.0 GB si visuel séparé, sinon natif `llama-cpp-python` multimodal |
| KV 8K Q8_0 | ~384 MB (vs 768 MB FP16) |
| Total lazy Q4_K_M+KV Q8_0 | **~0.98 GB** (vs 2.28 GB F16+Q8_0) |

> **Décision Réponses.md:133 + Complément:** Q4_K_M par défaut pour garder LFM2.5 résident (`5.5+0.98=6.48GB <8GB`). F16 uniquement si ≥12GB VRAM ou mode séquentiel strict avec accept 500ms reload (5.5+2.28=7.78GB + KV 1.5 = 9.28GB OOM si overlap).

---

### 3. LFM2.5-VL-3B (Tool vision généraliste)

**Fichier :** `models/Vision-general/LFM2.5-3B-UD-Q4_K_M.gguf`
**Architecture :** Liquid AI **dense** (backbone 2.6B + encodeur 400M SigLIP2 NaFlex) — **pas MoE** (cf LLM_vs_VLM.md)
**Rôle :** Description visuelle générale (objet, ambiance, style, écrans) — **VISION + TEXTE**

| Paramètre | Valeur |
|-----------|--------|
| Paramètres total | ~3.1B (2.6B + 400M) |
| Couches | 30 |
| Heads | 16 |
| Key-value heads | 8 |
| Dimension cachée | 2048 |
| Dimension feed-forward | 7168 |
| Contexte | **32K tokens MAX** (pas 128K !) |
| Vocabulaire | ~128 256 |
| Quantization | Q4_K_M |
| Fraction d'actifs | 100% (Dense) |
| Mémoire Q4 | ~2.5 GB |
| Mémoire FP16 | ~6.5 GB |
| KV Cache à 32K | <200 Mo (LIV, aucune explosion) |
| Encodeur visuel | SigLIP2 NaFlex 400M (ratios dynamiques) |
| Nb images | 1 à 5 simultanées avant saturation 32K |
| **Capacité vision** | **✅ OUI — Images + texte** |

### Récap & confusions à éviter (LLM_vs_VLM.md)

| Modèle | Rôle | Vision | Contexte | Archi | Mémoire Q4 |
|---|---|---|---|---|---|
| **LFM2.5-8B-A1B** | Orchestrateur + Générateur | ❌ Non (texte pur) | **128K** | MoE 32 experts top-4 (1.5B actifs) | 5.5GB |
| **GLM-OCR** | OCR technique | ✅ Oui | 8K-128K | Dense+MTP 0.9B | 0.6GB (Q4) / 1.9GB (F16) |
| **LFM2.5-VL-3B** | Vision généraliste | ✅ Oui | **32K** | Dense 3.1B (SigLIP2) | 2.5GB |

> **4 confusions corrigées:** 1) 8B ne traite **PAS** d'images → passer par VL-3B/GLM-OCR puis injecter texte dans 8B | 2) VL-3B ≠ 128K, c'est **32K max** | 3) VL-3B ≠ MoE, c'est **dense** | 4) 8B ≠ 1B actifs mais **1.5B** (A1B = marketing)

---

## Gestion mémoire (< 8 GB cible) — validée 30/08/2026

**Règle :** Chargement **séquentiel strict** — 1 seul modèle résident à la fois (borne 7.5GB, OOM 0%). `mmap` → chargement 300-500ms, déchargement 50-100ms. Léger overlap possible mais risqué.

| Modèles chargés | RAM/VRAM estimée (KV Q8_0) | Résultat |
|---|---|---|
| Chat seul (LFM2.5-8B 128K) | 5.5 + 6.0 = 11.5 GB | ❌ >8GB → **ctx adaptatif 32K par défaut = 7.0 GB** ✅ |
| Chat seul 128K natif | 11.5 GB | ⚠️ nécessite ≥12GB |
| Chat + vision (8B 32K + VL-3B) | 7.0 puis 2.5 (séquentiel) | ✅ séquentiel obligatoire |
| Chat + OCR Q4_K_M (8B 32K) | 7.0 ou 0.98 | ✅ séquentiel strict |
| Chat + OCR Q4_K_M (8B 128K) | 11.5 + 0.98 = 12.48 GB overlap | ❌ OOM → décharger 8B avant OCR |

**Borne garantie (séquentiel strict, 8B à 32K):** `max(7.0 GB LFM2.5 32K, 2.5 GB VL-3B 32K, 0.98 GB GLM-OCR) + 0.5GB = 7.5 GB` ✅ | **À 128K:** 12.0GB → nécessite ≥12GB ou ctx adaptatif

**Stratégie (ModelManager `contextmanager`):**

| Modèle | Chargement | Déchargement | Pattern |
|---|---|---|---|
| LFM2.5-8B | Boot / début requête | Garde chaud entre requêtes, décharge avant tool | `with manager.use_model("LFM2.5-8B") as llm:` |
| GLM-OCR Q4_K_M | `tool_call doc_ocr` | Immédiat après `tool_result` | lazy + `del + gc.collect()` |
| LFM2.5-VL-3B | `tool_call vision` | Immédiat après | lazy |

```python
# backend/inference/manager.py — séquentiel strict (cf Réponses.md Complément 2)
@contextmanager
def use_model(self, name: str):
    if self._current_name != name: 
        self._unload_current()  # del + gc.collect() + torch.cuda.empty_cache()
        self._current_model = self._load_model(name)  # Llama(use_mmap=True, flash_attn=True)
        self._current_model.create_completion("warmup", max_tokens=1)  # warmup
    yield self._current_model
```

**Timings mesurés (mmap + SSD NVMe):** LFM2.5-8B 300-500ms, GLM-OCR 100-200ms, VL-3B 150-300ms, overhead switch ~500ms (1.7% sur 5min).

### Visual tokens — piège à éviter

- **GLM-OCR :** 1 image 8K ≈ 10 000 visual tokens ≈ 384 MB KV Q8_0 (8K ctx). Résolution reco **200 DPI PNG** (PyMuPDF `pix.save`), pas 8K.
- **LFM2.5-VL-3B :** architecture LIV immunisée, KV <200MB à 32K, même 100 images pas d'explosion linéaire

---

## Inférence IPC Python — Cascade GPU (validée 30/08/2026)

> **Problème v0.8:** `Inférence.md` 4 bugs + `Rapport` — flags `--cuda` invalides, `n_gpu_layers` sur `free` au lieu de `total`, binaire Vulkan-only, double moteur. Phase 1 corrige via `llama-cpp-python`.

| Étape | Détail validé (Réponses.md) |
|---|---|
| **1. Ordre cascade** | **Par vendor, pas global:** `NVIDIA: CUDA→Vulkan→CPU` \| `AMD: ROCm→Vulkan→CPU` \| `Apple: Metal→CPU` \| `Intel: Vulkan→CPU` — Vulkan avant-dernier = fallback universel |
| **2. Wheels** | **PyInstaller sans venv:** `pip install llama-cpp-python --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu121 --force-reinstall --no-cache-dir` (choisir cu121/metal/vulkan selon plateforme), puis `pyinstaller --onedir`. Pas de `uv`, pas de `.venv` |
| **3. Vérif backend** | `from llama_cpp import llama_supports_gpu_offload; if not True → pip install --force-reinstall --no-cache-dir` — pas `ldd`/`--version` |
| **4. Formule layers** | **Simplifiée** (pas Ollama complet): `overhead=500MB; available=free-model-kv-overhead; per_layer=model/layers_total; return max(0, available/per_layer)` — robuste pour 1 modèle résident |
| **5. Fallback OOM** | `try Llama(n_gpu_layers=-1) except RuntimeError("CUDA out of memory"): partial=estimate_layers(); Llama(n_gpu_layers=partial)` → log `fallback_reason=vram_insufficient` |
| **6. Où détecter** | **Subprocess Python au boot** (`backend/inference/hardware.py:HardwareDetector.detect() → nvidia_smi() or rocm_info() or vulkan_info()`), expose `{"event":"backend_ready","payload":{backend, gpu, vram, n_gpu_layers}}` — pas Rust |
| **7. Install** | Wheels `cu121` par défaut (95% coverage compute 6.0+), `cu124` option RTX 4000+, jamais build source (`15min CMake fragile`) |

```
backend/inference/
  hardware.py  # HardwareDetector 5 étapes + estimate_layers()
  manager.py   # ModelManager séquentiel contextmanager + _load_model(use_mmap=True, flash_attn=True, type_k=8, n_keep=512)
  ipc.py       # emit()/log() + stdin loop
```

---

## Architecture IPC pour Tauri

> **Correction 30/08/2026 — Oubliez le SSE :** SSE = protocole HTTP. Dans Tauri il n'y a **pas de HTTP**. La stack réelle est **JSON Lines (Python→Rust sur stdout)** + **Tauri Events (Rust→Frontend via `window.emit`)**. Voir `Réponses.txt`.

### Communication frontend ↔ backend

```
Frontend (JS)  ←→  Rust (Tauri)  ←→  Python (stdin/stdout)
     ↑                  ↑
     │                  │
 Tauri events       JSON Lines (NDJSON)
 (window.emit)      (un JSON par ligne)
 listen()           json.dumps + "\n"
```

```
Frontend (React)                 Backend (Rust Tauri)              Backend (Python)
          │                                │                             │
          ├── invoke("chat_send", ...) ──> │                             │
          │                                ├── lance: python agent.py    │
          │                                │    stdin: JSON message      │
          │                                │                             │
          │                                │◄── stdout: JSON Lines        │
          │                                │    {"event":...,"payload":...}│
          │                                │    (un JSON par ligne)       │
          │                                │                             │
          │◄── listen("thinking") ────────│                             │
          │◄── listen("answer") ──────────│  emit(event, payload)       │
          │◄── listen("tool_call") ───────│                             │
```

**Flux de conversation :**
1. **User envoie un message** (via Tauri `invoke`)
2. **Rust lance le subprocess Python** (`backend/main.py`)
3. **Python émet des JSON Lines** sur stdout (`emit_event(type, payload)` → `json.dumps({event, payload}) + "\n"` + `flush()`)
4. **Rust lit stdout ligne par ligne** (`BufReader::lines()` → `serde_json::from_str` → `window.emit(event_type, payload)`)
5. **Frontend écoute** via `listen('thinking'|'answer'|'tool_call'|..., callback)` et affiche le streaming

**Pourquoi JSON Lines + Tauri Events > SSE :**
| Approche initiale | Problème | Solution retenue |
|---|---|---|
| A (JSON brut stream) | Pas un standard | NDJSON = standard éprouvé (Kafka, logs) |
| B (texte brut) | Format custom fragile | JSON typé robuste |
| C (SSE) | SSE = HTTP, n'existe pas en IPC | Tauri Events = natif, zéro HTTP |

Avantages : latence ~0.01ms (pipes vs 1-5ms HTTP), parsing trivial (3 lignes Python, 10 Rust, 1 JS), testable sans Tauri (`python backend/main.py < input.json`), types TS forts.

### Event format (JSON Lines) — validée Réponses.md #8-11

**Protocole validé:**

```
Rust → Python (stdin): {"id":"uuid","method":"chat.send","params":{"message":"...","mode":"agent","image":"base64?"}} \n
Python → Rust (stdout): {"event":"thinking","payload":{"text":"...","iteration":0}} \n  (une ligne = un JSON)
```

**Python — `backend/ipc.py`:**
```python
import os, sys, json
os.environ["PYTHONUNBUFFERED"] = "1"  # critique sinon events retardés
def emit(event: str, payload: dict):
    sys.stdout.write(json.dumps({"event": event, "payload": payload}, separators=(",", ":")) + "\n")
    sys.stdout.flush()
def log(msg: str):  # logs → stderr, jamais stdout
    sys.stderr.write(msg + "\n"); sys.stderr.flush()
# stdin loop
for line in sys.stdin:
    req = json.loads(line)  # {id, method, params}
```

**Rust parse** → `BufReader::lines()` → `serde_json::from_str` → `window.emit(event_type, payload)`
**Frontend reçoit :**
```javascript
listen('answer', (e) => appendToChat(e.payload.text))
listen('thinking', (e) => appendThinkingBlock(e.payload.text))
listen('tool_call', (e) => showToolLoading(e.payload.tool))
```

**Lifecycle (Réponses.md #11):** PyInstaller `--onedir` (pas `--onefile` → bug bootloader double PID). Rust `kill(Pid::from_raw(pid), SIGTERM)` pas `child.kill()`. `Payload 100KB tient en 1 ligne JSON`, pas besoin TCP sauf >10MB.

Types d'events supportés (7, validés #17) :
- `message_started` — `{request_id, run_id, model}`
- `thinking` — `{request_id, text, iteration}`
- `tool_call` — `{request_id, tool_call_id, tool, arguments}`
- `tool_result` — `{request_id, tool_call_id, result, ok, latency_ms}`
- `answer` — `{request_id, text}`
- `message_completed` — `{request_id, run_id, tokens_in, tokens_out, total_latency_ms, sources?}`
- `error` — `{request_id, message, code}`
- `backend_ready` — `{backend, vram, n_gpu_layers}` (hardware)

**Contrat IPC final (Réponses.md §4 filtré) :**
- **Pattern** : Frontend `invoke("chat_send", {request_id, message, mode, image_paths?})` → Rust `#[tauri::command]` écrit stdin Python → Python stdout JSON Lines → Rust `BufReader::lines()` → `window.emit(event, payload)` → Frontend `listen(event, cb)`. Ne PAS utiliser `Command.sidecar()` direct depuis frontend. `cancel_request` = **Phase 2** (forward `{"method":"cancel"}` → worker `cancel_flag`, non MVP).
- **Typage** : `frontend/src/types/ipc.ts` Zod schemas (`MessageStartedSchema`, `ThinkingSchema`...) + `z.infer`, `Schema.parse(event.payload)` à chaque `listen`.
- **Parallélisme** : `request_id = crypto.randomUUID()` corrélé sur tout le cycle `message_started → message_completed` pour filtrer les events si 2 chats parallèles.
- **Stack** : **React 19 uniquement** (l'outil recherche propose Vue/Svelte générique, ignoré — notre contexte = React 19).

---

## Frontend (React/Vite + Tauri)

Pages minimales en v1 :

| Page | Rôle | Composants |
|------|------|-----------|
| **ChatHome** | Liste conversations + nouveau | Sidebar, bouton "Nouveau", list conversations |
| **ChatConversation** | Affiche + compose message | Liste messages, thinking blocks, composer |
| **AgentHome** | Liste conversations (mode agent) | Identique à ChatHome + toggle mode |
| **AgentConversation** | Idem avec tool results | `ToolCallDisplay.tsx` pour vision/OCR |

**UI Tool (Réponses.md §6)** : `ToolCallDisplay({tool, status, result})` — `running: <Spinner> "🔍 Analyse image..." / "📄 OCR page 3/50..."` , `completed: <details><summary>✅ vision terminé (N chars)</summary><pre>preview 500 chars</pre></details>` , `error: "❌ doc_ocr a échoué: timeout"`. Zod validation `MessageStartedSchema` etc., `request_id` corrélé.

Pas de pages spaces, gallery, history, images, settings en v1 (simplifier au minimum).

---

## Flux Backend (Python) → Tauri (Rust) → Frontend (React)

> Format mis à jour 30/08/2026 : **JSON Lines** `{"event": "...", "payload": {...}}` + **Tauri `emit`/`listen`** (pas `emit("streaming")`, pas SSE).

### Chat simple (sans outil)

| Frontend | Tauri (Rust) | Python (Agent) |
|-----------|---------|--------|
| User envoie un message | | |
| ──invoke("chat_send")──> | | |
| | │ lance python agent.py |
| | │ stdin: {"message":"..."} |
| | │┌── stdout JSON Lines ──┐ |
| | ││ {"event":"message_started","payload":{"run_id":"...","model":"LFM2.5-8B"}} │ |
| | ││ {"event":"thinking","payload":{"text":"...","iteration":0}} │ |
| | │ │└────────────────────────┘ |
| │ ◄──listen("thinking")──────│ │▸ emit("thinking", payload) |
| │ ◄──listen("answer")────────│ │▸ answer chunks {"event":"answer","payload":{"text":"..."}} |
| | │ │{"event":"message_completed","payload":{"run_id":"...","tokens_in":..}}│ |
| | └── | |
| | ▸ conversation sauvegardée | |

### Agent mode (avec outils)

| Frontend | Tauri (Rust) | Python (Agent) |
|-----------|---------|--------|
| User envoie un message | | |
| ──invoke("agent_send")──> | | |
| | │ lance python agent.py |
| | │ stdin: {"message":"...","mode":"agent"} |
| | │┌── orchestrator loop: ─┐ |
| | │ │▸ decide: need tools? |
| | │ │▸ appelle: vision |
| | │ │ │{"event":"tool_call","payload":{"tool":"vision","tool_call_id":"...","arguments":{}}} |
| | │ │ │▸ passe l'image → modèle LFM2.5-VL-3B |
| | │ │ │▸ reçoit: description textuelle |
| | │ │ │{"event":"tool_result","payload":{"tool_call_id":"...","result":"...","ok":true}} |
| | │ │▸ décide: besoin d'OCR ? |
| | │ │▸ appelle: doc_ocr |
| | │ │ │{"event":"tool_call","payload":{"tool":"doc_ocr",...}} |
| | │ │ │{"event":"tool_result","payload":{"tool_call_id":"...","result":"...","ok":true}} |
| | │ │▸ brief final |
| | │ │▸ generator en streaming (NO tools) |
| | | │▸ {"event":"thinking",...} + {"event":"answer",...} |
| | → |▸ listen("tool_call"/"tool_result"/"answer") |

---

## Pipeline d'images & fichiers — validé Réponses.md §5

### Vision / Doc OCR — flux fichier

- **Upload** : Frontend `file input` → Rust `save_upload(request_id, filename, data: Vec<u8>)` valide `MAX_IMAGE 5MB / MAX_PDF 20MB` + magic bytes `89PNG / FFD8FF / %PDF` (Rust rapide), double défense Python `python-magic` `magic.from_buffer(data, mime=True) in ALLOWED_MIMES` (précis), sauve `/tmp/thot/{request_id}/{sha256}.{ext}` via `Path(tempfile.gettempdir())/thot`, retourne `(path, sha256:hash)` → `invoke("chat_send", {request_id, image_paths:[path]})` → Python lit `file://path`. Garde-fou `Path(filename).name` (strip `../`), `resolve().startswith(THOT_TMP)`, `suffix` whitelist.
- **1 image ou 1 PDF / message**, max 50 pages/PDF Phase 1. `hash` SHA-256 pour dedup/cache OCR.
- **Cleanup** : `finally: cleanup_request(request_id) → assert path != THOT_TMP and startswith → shutil.rmtree(..., ignore_errors=True)` après `message_completed` (même si erreur), RGPD éphémère (`/tmp` systemd nettoie 10j). `data/uploads/` persistant non utilisé Phase 1.

### Vision tool

- `LFM2.5-VL-3B` lit `image_path` direct, `SYSTEM_VISION` + `temp 0.2`, 1 à 5 images max avant 32K saturation.

### Doc OCR tool

- **PyMuPDF** `fitz.open(pdf_path)` → `page.get_pixmap(dpi=200)` → `pages/{i}.png` PNG lossless, **page par page séquentiel** (pas batch, évite 384MB/page 8K KV blowup) → `"\n\n--- Page suivante ---\n\n".join(results)`. Checkpoint tous les 5 pages si >20 pages.
- OCR `temp 0.0` déterministe, `max_tokens 8192`.

---

## Configuration — validée Réponses.md #19-21

### Chemins de modèles

Les modèles sont stockés **localement** `models/` (placement manuel GGUF avant launch):

- LFM2.5-8B : `models/LLM/LFM2.5-8B-AB-UD-Q4_K_M.gguf` (5.5GB)
- GLM-OCR : `models/Vision-document/GLM-OCR-Q4_K_M.gguf` (0.6GB) — F16 1.9GB uniquement si ≥12GB
- LFM2.5-VL-3B : `models/Vision-general/LFM2.5-3B-UD-Q4_K_M.gguf` (2.5GB)

### Config des modèles — frozen dataclasses

```python
from dataclasses import dataclass
@dataclass(frozen=True)
class ModelConfig:
    path: str
    n_ctx: int
    n_gpu_layers: int = -1  # auto via HardwareDetector
    flash_attn: bool = True
    type_k: int = 8; type_v: int = 8  # KV Q8_0
    n_keep: int = 512  # prompt caching
    use_mmap: bool = True; use_mlock: bool = False
    supports_vision: bool = False  # ← CRITIQUE (LLM_vs_VLM.md): 8B = False, VL/OCR = True

MODEL_CONFIGS = {
  "LFM2.5-8B": ModelConfig(path="models/LLM/LFM2.5-8B-AB-UD-Q4_K_M.gguf", n_ctx=131072, supports_vision=False),  # 128K natif, texte pur ❌ vision — adaptatif 32K si 8GB VRAM
  "GLM-OCR": ModelConfig(path="models/Vision-document/GLM-OCR-Q4_K_M.gguf", n_ctx=8192, flash_attn=False, supports_vision=True),
  "LFM2.5-VL-3B": ModelConfig(path="models/Vision-general/LFM2.5-3B-UD-Q4_K_M.gguf", n_ctx=32768, supports_vision=True),  # 32K MAX, dense, vision ✅
}
# Vérif: if not MODEL_CONFIGS[name].supports_vision and "image" in req: raise ValueError("8B texte pur, utilisez VL-3B/GLM-OCR")
```
Hardcodé, pas de `.env`/`BaseSettings` Pydantic — `frozen=True` = zéro surface d'attaque config (comme v0.8 0.9.3). Warmup `create_completion("warmup", max_tokens=1)` après chaque load, tokenizer préload global.

**`n_ctx` adaptatif (Réponses.md §5) :**
```python
def resolve_n_ctx(model_name: str, free_vram: int, estimated_tokens: int) -> int:
    GB = 1024
    if model_name == "LFM2.5-8B":
        n_ctx = 131072 if free_vram >= 12*GB else min(32768, max(4096, estimated_tokens+1024))
    elif model_name == "LFM2.5-VL-3B": n_ctx = 32768
    elif model_name == "GLM-OCR": n_ctx = 8192
    else: raise ValueError(model_name)
    log.info(f"n_ctx_resolved={n_ctx} free_vram={free_vram}MB model={model_name}")
    return n_ctx
def estimate_tokens(text: str) -> int: return len(text)//4  # 95% rapide, tokenizer exact pour compaction
# flash_attn+Q8_0: try Llama(..., flash_attn=True, type_k=8, type_v=8) except RuntimeError("flash"): fallback flash_attn=False
```

### Packaging — PyInstaller

```bash
pyinstaller --onedir --name thot-sidecar backend/main.py  # pas --onefile (bug PID)
# → dist/thot-sidecar/ → src-tauri/binaries/thot-sidecar-<triple>/
# tauri.conf.json: bundle.externalBin = ["binaries/thot-sidecar"]
# capabilities/default.json: shell:allow-execute sidecar:true
```

### Dev workflow — PyInstaller sans venv (validé)

```bash
# Modèles — déjà déplacés depuis Thot v0.8, rien à télécharger
ls -lh models/LLM/ models/Vision-document/ models/Vision-general/

# Dépendances (sans venv, pip système)
pip install -r requirements.txt
# Pour GPU CUDA si wheel CPU par défaut :
# pip install llama-cpp-python --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu121 --force-reinstall --no-cache-dir

# Test Python sidecar seul (JSON Lines)
echo '{"id":"test","method":"chat.send","params":{"message":"Bonjour","mode":"chat"}}' | python backend/main.py

# Packager le sidecar puis Tauri dev
pyinstaller --onedir --name thot-sidecar backend/main.py --distpath src-tauri/binaries/ --clean --noconfirm
npm install && npm run tauri dev  # remappe externalBin → src-tauri/binaries/thot-sidecar/ en dev

# Build prod
pyinstaller --onedir --name thot-sidecar backend/main.py --distpath src-tauri/binaries/ --clean --noconfirm
npm run tauri build  # bundle sans models/ (via .taurignore)
```
*Pas de `uv`, pas de `.venv` — PyInstaller packagé en binaire autonome, comme demandé.*

### Base de données — fresh start 4 tables (Réponses.md §3)

`data/thot-phase-1.db` **fresh start** (pas d'import `thot.db` v0.8, 9 tables legacy = dette) :

```sql
CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT, mode TEXT, created_at DATETIME, updated_at DATETIME);
CREATE TABLE messages (id TEXT PRIMARY KEY, conv_id TEXT REFERENCES conversations(id) ON DELETE CASCADE, role TEXT, content TEXT, thinking TEXT, run_id TEXT, status TEXT, tokens_in INT, tokens_out INT, created_at DATETIME);
CREATE TABLE toolcalls (id TEXT PRIMARY KEY, msg_id TEXT REFERENCES messages(id) ON DELETE CASCADE, tool_call_id TEXT, tool TEXT, args JSON, result TEXT, ok BOOLEAN, latency_ms INT); -- args={"image_hash","image_path","query"} jamais base64
CREATE TABLE sources (id TEXT PRIMARY KEY, msg_id TEXT REFERENCES messages(id) ON DELETE CASCADE, source_type TEXT, path TEXT, hash TEXT);
CREATE INDEX idx_messages_conv_created ON messages(conv_id, created_at);
CREATE INDEX idx_toolcalls_msg_id ON toolcalls(msg_id);
CREATE INDEX idx_sources_msg_id ON sources(msg_id);
```

SQLAlchemy `Column(JSON)` (SQLite TEXT + JSON1), `relationship(..., cascade="all, delete-orphan")`, `ForeignKey(ondelete="CASCADE")`. Tool traces `ephemeral` : persistés mais exclus des prompts futurs.

---

## Optimisations validées (Réponses.md Compléments)

| Optimisation | Décision | Gain |
|---|---|---|
| KV Cache Q8_0 + flash_attn ON | Validé #15 | -50% KV, vitesse 128K |
| Prompt caching `n_keep=512` | Validé | -200ms/request (system prompt) |
| Model warmup `max_tokens=1` | Validé | évite 2s première inférence |
| Tokenizer preload global | Validé | -50ms |
| Checkpoint OCR >20 pages | Validé | resume après crash |
| Speculative DSpark 328M | **Phase 2 si ≥10GB** | 2.5x speedup |
| Router mode llama.cpp | **Phase 2** | multi-modèles natif |
| Adaptive n_ctx | **Phase 2** | KV proportionnel |

---

## Ce qui est hors scope Phase 1

- RAG (ChromaDB, embeddings, document pipeline)
- Web search (DuckDuckGo, SearXNG)
- Memory (fact extraction, profile memory, compaction)
- Skills (loader, registry, prompt injection)
- Toolsets complexes (CHAT/SPACE/AGENT, permissions, policy, enforcement)
- Compressor contextual (TurnState, TurnGuard, budget, compression automatique)
- Security guards complets (sanitization anti-prompt-injection)
- Observability (metrics, tracing, redact, insights, rate limit)
- CLI (interface interactive)
- Backend API REST (tout est IPC Tauri)

---

## Contraintes

- Ne **jamais** écrire dans le code existant (`Thot v0.8/` n'est PAS modifié)
- `Futur/` est un dossier de brouillon — seul `Thot-phase-1/` contiendra le code final
- `Thot-phase-1/` est un **produit standalone**
- Le code utilise **llama-cpp-python** (installable, cross-platform)
- Communication **IPC via Tauri** (pas de HTTP/REST entre frontend et backend)
- Une **nouvelle base SQLite** dédiée (`data/thot-phase-1.db`)
- **Python subprocess** géré par Rust Tauri pour l'exécution des modèles
