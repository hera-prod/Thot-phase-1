# Modèles + Stockage portable + Build

> Note : les renvois `docs/...`, `q-r.md` et à la « Bible » désignent la documentation interne du projet ; elle n'est pas incluse dans l'export HuggingFace.

## 1. Dossiers englobés

```
models/                          ← copié à côté de l'app, poids NON inclus dans le dépôt (LLM 1 + Vision-general 4 + PaddleOCR-VL-1.6 + ColBERT + flux2-klein ; PP-DocLayoutV3 prévu, non livré)
├── LLM/LFM2.5-8B-A1B-UD-Q4_K_M.gguf (5,0 Go, MoE 8.3B 32 top-4, 128K natif, supports_vision=False)
├── PaddleOCR-VL-1.6/ (1,8 Go safetensors, worker transformers persistant — c'est le moteur OCR livré ; GLM-OCR n'a jamais été livré et est abandonné)
├── colbert/LFM2.5-ColBERT-350M/ (0,68 Go — recherche sémantique, voir §1bis)
├── PP-DocLayoutV3_safetensors/ (Prévu, sous licence Apache-2.0, pas encore branché au pipeline OCR)
├── Vision-general/LFM2.5-VL-3B-UD-Q4_K_XL.gguf (1,6 Go) + mmproj (BF16 0,8 Go recommandé ; variantes F16 0,8 Go et Q8 0,6 Go)
    (3.1B dense, 32K, NaFlex 512×512 patches + thumbnail, supports_vision=True, flash_attn=True)
└── flux2-klein/ (14,8 Go — moteur image `backend/image_engine/`, T2I officiel ~1 MP, pic VRAM mesuré 3,06 Go)
    ├── transformer/flux2-klein-9b-kv-Q4_K_M.gguf (9,6 Go) + Flux-2-Klein-9B-KV-Q3_K_M.gguf (variante <8 Go)
    ├── text_encoder/Qwen3-8B-Q4_K_M.gguf (5,0 Go, vocabulary 151936 compatible Klein)
    ├── vae/ (0,2 Go safetensors + config)
    └── tokenizer/ (tokenizer.json + chat_template.jinja)
```

data/ (portable si portable.json à la racine → <racine>/data, sinon ~/.local/share/thot/data)
├── database/thot-phase-1.db     ← SQLite WAL — 25 tables métier + schema_migrations + 2 tables FTS (artifact_search, chunk_search) (cf. backend/db/README.md)
├── blobs/                       ← durable, dédupliqué par sha256, jamais renommé —
│                                  layout réel SHARDÉ : <2 hex>/<sha256>.bin (dépôt pièces
│                                  jointes, ressources web) ; variante <sha256>/<2 hex>/<sha256>.bin
│                                  (images générées flux) ; reste à plat <sha256><ext>
│                                  (compat historique image_paths — cf. backend/db/README.md §4)
├── logs/                        ← journaux du sidecar
├── temp/                        ← volatil ; temp/staged/ pour le staging pièces jointes
└── backups/                     ← sauvegardes

Pas de previews/, pas de trash/ : les vignettes sont reconstruites à la
demande, et la corbeille est un état SQL (backend/search/trash.py) — 7 jours, puis purge de
l'ArtifactLink et GC du blob s'il devient orphelin. MAIS `data/derived/figures/` existe
(backend/ingestion/figures_etape.py:498) : figures extraites, reconstruites à la demande —
et `data/staged/` sert au staging des effets image (backend/tools/generate_image.py:387),
`data/temp/staged/` à celui des pièces jointes (backend/ipc/attachments.py). Les poids
vivent dans models/, À LA RACINE de l'app (hors data/), recherchés portable > bundle >
CWD > app_data.

racine : portable.json + distribution.json + licenses/ (versionnés)
         pyproject.toml + requirements.txt (llama-cpp-python==0.3.34 figé) ;
         les lanceurs lancer-*.sh/.command/.bat créent un .venv au premier lancement
         et y installent les dépendances (pas d'uv)
         pyinstaller --onedir --collect-all backend backend/main.py  ← --onedir imposé par pyproject.toml (pas --onefile, aucun .spec livré)
         frontend/public/fonts/ : Manrope (manrope-400/500/600/700.woff2 + OFL.txt),
         police embarquée localement — aucun appel réseau au premier rendu

### Pic VRAM du moteur image : 3,06 Go, et ce n'est pas un gain

L'arbre ci-dessus dit « pic VRAM mesuré 3,06 Go » ; avant le 05/10/2026 il y disait
« ~7,9 Go ». **Le pic n'a pas rétréci.** Ce qui était faux, c'est la répartition
entre RAM et VRAM, et le chiffre 7,9 était la conséquence de cette répartition
fausse :

| grandeur | 05/10, avant | 06/10, aujourd'hui |
|---|---|---|
| `RssAnon` — RAM anonyme | **6,9 Go**, dont les 5,45 Go du fichier recopiés | **1,50 Go** |
| VRAM pic `max_memory_reserved` (la convention) | non relevée | **3,06 Go** |
| cumul de convention `RssAnon` + VRAM pic | **8,9 Go** → hors budget, Bible §1.4 | **4,10 Go** → sous les 8 Go |

`RawVRAMIndex` servait les poids quantifiés depuis `torch.from_numpy(arr.copy())` :
une **recopie des 5,45 Go du fichier de poids en mémoire anonyme**. Le même octet
était donc payé deux fois — une fois en RAM (parmi les 6,9 Go), une fois en VRAM
après déquantisation. La VRAM ne pouvait pas être basse : elle portait sa part du
poids ; 7,91 Go n'était pas un pic, c'était la signature de ce double compte. Mapper
le fichier (`mmap` MAP_PRIVATE, sans copie) fait passer la charge dans la catégorie
qui l'exclut du cumul (`RssFile` : un `mmap` et la déquantisation de ce même bloc sont
deux états successifs du MÊME octet — `mempwatch.CONVENTION_MESURE`), et
`LazyLinear.forward` relâche ensuite ces pages pour que le processus ne se facture
pas le fichier qu'il ne fait que lire. Mesures du 06/10 à 1024², Q4_K_M, 4 pas,
sur le run réel de `backend/image_engine/spike_t2i.py` ; seuil d'admission dérivé de
la même mesure dans `backend/services/flux_spike.py`.

## 1bis. ColBERT — le modèle de recherche sémantique

`colbert/LFM2.5-ColBERT-350M/` (0,68 Go, **résident, jamais déchargé**).
Chargé par `backend/search/vectors.py`, qui rend `last_hidden_state` — un vecteur
**par jeton**, pas par phrase. Score : `Σ_jetons_requête max_jetons_document(q·d)`.

### Pourquoi il est RÉSIDENT, et pas déchargé entre deux requêtes

Le chemin de discussion tient 6,66 Go de VRAM (poids 4,96 + cache KV 0,80 +
tampon calcul ~0,90). ColBERT en bf16 ajoute **685 Mio mesurés**, pour **7,35 Go**
au total : sous le plafond de 8 Go de la Bible (1.4), avec 0,65 Go de marge.

Un cycle de déchargement ne ferait donc RIEN gagner en mémoire ici, et coûterait une
relecture du fichier de poids (706 661 112 o, soit 674 Mio) à chaque recherche.

> **L'argument « forcé » qui justifiait le motif séquentiel du chemin IMAGE ne tient
> plus.** Il disait : 5,32 Go (poids) + 4,96 Go (LLM) dépassent 8 Go, donc il faut
> décharger avant de charger. Mais les 5,32 Go sont un fichier **mmapé et relâché**,
> donc hors `RssAnon` par la convention — le cumul mesuré du chemin image est
> **4,10 Go** à 1024². Le séquentiel est donc aujourd'hui une précaution, pas une
> nécessité démontrée : personne n'a mesuré le chemin image *sans* séquentiel.
> Le retirer est une décision d'architecture à instruire, pas un commentaire à
> réécrire. Elle est ouverte.

### Précision : bf16 sur GPU, fp32 sur CPU

353,3 M paramètres. bf16 = 685 Mio de VRAM ; fp32 = 1359 Mio. En fp32 le total
monterait à 8,02 Go — **au-delà du plafond**. bf16 n'est pas un compromis de
vitesse : c'est la précision d'entraînement du checkpoint
(`config.json: dtype bfloat16`), donc la valeur correcte. Sur CPU on garde fp32,
où bf16 serait plus lent ET moins juste.

### Mesures (RTX 4070 / CPU, ce dépôt)

| | GPU bf16 | CPU fp32 |
|---|---|---|
| charge | 0,52 s | 0,48 s |
| encodage d'un fragment | 14 ms | 78 ms |
| encodage d'une requête | 14 ms | 71 ms |
| MaxSim, 13 fragments | 0,7 ms | 0,08 ms |
| VRAM résidente | **685 Mio** | — (RAM +1,4 Go) |
| recherche complète (corpus réel) | 77 ms | ~330 ms |

`models.safetensors` fait 706 661 112 o (≈ **0,68 Go**), tête dense + tokenizer
inclus : dossier **≈ 0,68 Go** au total.

### Réindexation — OBLIGATOIRE et EXPLICITE

Le format a changé : 256 flottants signés par fragment → **128 vecteurs de jetons**
par fragment. Un vecteur 256-dim déjà en base ne se compare pas à des vecteurs de
jetons ; il n'a pas la même nature. Rien ne se réindexe tout seul.

```bash
# 1. CONSTATER (lecture seule — le DÉFAUT, ne touche à rien)
python3 -c "
from backend.db.database import SessionLocal
from backend.search import vectors as V
print(V.statut_moteur()); print(V.detecter_vecteurs_obsoletes(SessionLocal()))"

# 2. RÉPARER (volontaire, borné, signalé)
python3 -c "
from backend.db.database import SessionLocal
from backend.search import vectors as V
db = SessionLocal(); print(V.reindexer_vecteurs(db, appliquer=True, limite=200)); db.commit()"
```

Le rapport rend `detectes`, `reindexes`, `a_reindexer`, `limite_atteinte`.
`appliquer=False` est le défaut : inspecter ne réécrit rien.

### Volume mesuré, et le coût du rechargement

Un fragment ColBERT fait 145 à 510 jetons (341 en moyenne) contre 1 vecteur avant.
Sur les 13 fragments du corpus réel : **13 Kio → 1,11 Mio, soit ×85,5**.
Projeté sur les 217 artefacts du dépôt : **~18 Mio** d'index, contre 0,2 Mio.

C'est cette multiplication par ~85 qui rend le balayage complet intenable — et la
raison pour laquelle `search_vectors()` filtre désormais **en SQL** (portée
appliquée à la jointure) au lieu de tout lire puis de trier en Python.

### Ce que la recherche devient

Le FTS **reste**. Les trois signaux sont fusionnés par RRF (k=60) :

| signal | répond à |
|---|---|
| FTS document (BM25) | « quel nom contient ce fichier ? » |
| FTS par fragment (BM25) | « quels mots contient ce passage ? » |
| **ColBERT (MaxSim)** | « de quoi parle ce fragment, quels que soient les mots ? » |

Mesuré sur des requêtes **paraphrasées sans aucun mot plein commun** : ColBERT
acc@1 = 0,88 / MRR 0,93 contre BM25 acc@1 = 0,19 / MRR 0,34. Sans recouvrement
lexical, BM25 ne rend rien : c'est ce que la Bible 2.5 désigne comme manquant.

**Seuils et libellés : rien ne change, et c'est délibéré.** Le score de fusion
reste un RRF normalisé combiné au score lexical (0,6 / 0,4). ColBERT n'entre pas
comme un score absolu dans le seuil de publication, mais comme un RANG dans la
liste — donc aucun seuil de `fts.py` n'est modifié.

Remplacer un seuil calibré sur l'ancien moteur exigerait une mesure de rappel sur
un corpus annoté, et ce dépôt n'en a pas. Changer le seuil SANS cette mesure
serait annoncer une amélioration sans l'avoir vérifiée — l'erreur même que ce
travail répare ailleurs.

## 2. Pseudo-code (comment c'est fait)

```
# Résolution stockage (Electron source de vérité, Python suit via env)
data_dir = SI portable.json présent → <racine>/data
         SINON → ~/.local/share/thot/data (XDG)
db = data/database/thot-phase-1.db
Electron lance le sidecar avec THOT_DATA_DIR / THOT_DB_PATH / THOT_APP_DATA_DIR
Python database.py : env prioritaire SINON get_data_dir()/database/thot-phase-1.db

# Gestion mémoire (cf. backend/inference/README.md pour la FSM complète)
RÈGLE : 1 modèle LOURD en VRAM à la fois, admission 1 run lourd
  (+ ColBERT, léger et RÉSIDENT : 685 Mio, cf. §1bis — il ne compte pas comme
   modèle lourd et ne se décharge pas)
  orchestrator/générateur : LFM2.5-8B (temp 0.2, n_ctx adaptatif ≥8 Go→131072 · ≥7 Go→32768 · sinon min(32768, max(8192, tokens+2048)))
  vision générale : LFM2.5-VL-3B + mmproj (prepare 1536px JPEG88, NaFlex tracé)
  OCR : PaddleOCR-VL-1.6 via worker persistant (tâches OCR:/Table Recognition:, bfloat16) — le moteur livré ; GLM-OCR n'a jamais été livré, abandonné
  ColBERT : RÉSIDENT (bf16, 685 Mio) — PAS de déchargement, cf. §1bis
  KV Q8_0, mmap, _wait_for_vram_release(3s), unload sleep 0.5s + attente 5s
  OOM ("Failed to load model" ou VRAM basse) → n_gpu_layers réduit + n_ctx /2 (32768→16384→8192...)

# Build
dev :
  pip install --break-system-packages -r requirements.txt
  python3 -m backend.main < ligne JSON  # test sidecar sans Electron
  npm --prefix frontend install && npm run build
  npm run dev  # Electron + Vite (drag natif, preview token)
prod :
  pyinstaller --onedir --collect-all backend backend/main.py  # --onedir imposé par pyproject.toml
  npm run build  # frontend Vite
  npm run dist  # electron-builder : AppImage linux x64 — bundle SANS models/ — modèles externes
```

## 2bis. Schémas ASCII

```
STOCKAGE (portable vs installé — même code, racine différente)
──────────────────────────────────────────────────────────────
 portable.json présent ? ──OUI──▶ <racine>/data/           (clé USB / dossier app)
         │                        ├─ database/thot-phase-1.db (WAL, 25 tables métier + schema_migrations + 2 tables FTS artifact_search/chunk_search)
         NON                      ├─ blobs/ <2 hex>/<sha256>.bin shardé (durable, dédupliqué ;
         │                        │    variantes : <sha256>/<2 hex>/ (flux) et <sha256><ext> à plat (legacy))
         │                        ├─ logs/ (journaux du sidecar)
         ▼                        ├─ temp/ (volatil ; temp/staged/ pour le staging)
 ~/.local/share/thot/data/ (XDG)  ├─ backups/ (sauvegardes)
         │                        └─ (pas de previews/, pas de trash/ : vignettes reconstruites, corbeille = état SQL ;
         │                            derived/figures/ existe : figures extraites, reconstruites à la demande)
         └─ même arborescence ─────▶ (chemins relatifs + sha256 → T-ATT-18 OK)

models/ (LLM 1 + Vision-general 4 + PaddleOCR-VL-1.6 + ColBERT + flux2-klein — poids NON inclus ;
≈ 25 Go au total une fois installés, dont flux2-klein ≈ 14,8 Go ; hors PP-DocLayoutV3 (prévu) ;
GGUF/safetensors ignorés git, README versionné)
  ├─ LLM/LFM2.5-8B-A1B-UD-Q4_K_M.gguf ............ 5,0 Go (orchestrateur+générateur, temp 0.2)
  ├─ PaddleOCR-VL-1.6/model.safetensors .......... 1,8 Go } OCR (worker transformers persistant, jamais llama.cpp)
  ├─ colbert/LFM2.5-ColBERT-350M/ ............... 0,68 Go } RÉSIDENT, bf16, 685 Mio VRAM
  │   (late interaction, 128 dim/jeton, MaxSim — cf. §1bis)                    }
  ├─ Vision-general/LFM2.5-VL-3B-UD-Q4_K_XL.gguf . 1,6 Go } vision 32K NaFlex
  │   + mmproj (BF16 0,8 Go · F16 0,8 Go · Q8 0,6 Go) ...... }
  └─ flux2-klein/ (transformer 9,6 + text_encoder 5,0 + vae 0,2 + tokenizer) . 14,8 Go — moteur image
```

```
TIMELINE VRAM (séquentiel strict — 1 modèle lourd à la fois, ColBERT RÉSIDENT)
─────────────────────────────────────────────────────────────────────────────
temps ──────────────────────────────────────────────────────────────────▶
 [8B orchestre] → [unload 0.5s+5s] → [VL-3B vision] → [unload] → [8B génère]
       6,66 Go gardé chaud        │         │                       │
  test manuel SKIP si <6 Go ─────┘         │ (en-app OK : 1 sidecar)
        │
        └─ ColBERT 685 Mio reste GARDÉ tout le temps (§1bis) :
           discussion 6,66 Go + 685 Mio ≈ 7,35 Go < 8 Go. Pas de cycle : rien
           ne déborde, et recharger coûterait 674 Mio par recherche.
  libres (conflit 2 processus,            │  _wait_for_vram_release(3s)
  pas un bug)                             │  KV Q8_0 · mmap

BUILD (dev vs prod)
 dev  : pip install -r requirements.txt → pytest → python3 -m backend.main (test JSONL)
        → npm --prefix frontend install && build → npm run dev (Electron + Vite)
 prod : pyinstaller --onedir --collect-all backend backend/main.py
        → npm run build → npm run dist (electron-builder AppImage linux x64, bundle SANS models/)
```

## 3. Pseudo-code et extraits de référence

```bash
pip install --break-system-packages -r requirements.txt
python3 -m pytest tests/backend/ -q --ignore=tests/backend/manual -k "not slow"
echo '{"id":"test","method":"chat.send","params":{"message":"Bonjour","mode":"chat"}}' | python3 -m backend.main
npm run dev  # Electron + Vite
```

```python
# backend/config/defaults.py — pseudo-code de référence (clés réelles = noms des modèles)
MODEL_CONFIGS = {
  "LFM2.5-8B"        : orchestrateur + générateur (temp 0.2, n_ctx 131072, supports_vision=False),
  "LFM2.5-VL-3B"     : vision générale (n_ctx 32768, mmproj BF16, flash_attn),
  "PADDLEOCR-VL-1.6" : OCR via worker transformers persistant (n_ctx 8192 indicatif, jamais llama.cpp),
}
resolve_n_ctx(free_vram, estimated_tokens) → 131072 si ≥8 Go libres · 32768 si ≥7 Go
                                           · sinon min(32768, max(8192, estimated_tokens + 2048))
```

## 4. Pièges

- Test VL manuel SKIP si < 6 Go libres (8B ≈ 6,66 Go gardé chaud dans le même processus applicatif — en-app pas de conflit car un seul sidecar séquentiel).
- T-ATT-18 (déplacement portable) : blobs survivent car chemins relatifs + sha256, jamais absolus en DB cible.
- **ColBERT ne se décharge pas** (§1bis). Le motif séquentiel existe pour les modèles LOURDS ; appliquer « décharger avant de charger » à ColBERT coûterait 674 Mio par recherche pour zéro octet gagné.
- **ColBERT sans réindexation classe sur du vide** (§1bis). Les vecteurs 256-dim ne sont pas « approximatifs » : ils sont d'une autre nature. Un `reindexer_vecteurs(..., appliquer=True)` explicite est requis après le premier démarrage qui suit ce changement.
- **ColBERT ne se télécharge pas tout seul.** Le dossier est lu dans `models/`, jamais sur le hub. Un dossier absent fait fonctionner la recherche **lexicale** — et `vectors.statut_moteur()` le dit au lieu de laisser croire à une recherche sémantique muette.
- `transformers` est importé par le dépôt depuis PaddleOCR-VL et l'encodeur FLUX, et n'était **pas déclaré** avant la tâche 2.5 : une installation depuis `requirements.txt` seul ne pouvait pas démarrer l'OCR. Déclaré désormais dans les deux manifests.
