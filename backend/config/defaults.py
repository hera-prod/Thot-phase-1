# Configuration figée Phase 1 — aucun .env, aucun BaseSettings
# Tout est hardcodé en frozen dataclass (zéro surface d'attaque config)
from dataclasses import dataclass

# Ratio conservateur d'estimation des tokens : 3 caractères/token. Le français
# tokenise souvent plus finement que 4 ; sous-estimer coûte un dépassement de
# fenêtre, sur-estimer ne coûte qu'une compaction plus tôt.
CHARS_PER_TOKEN = 3


@dataclass(frozen=True)
class ModelConfig:
    """Un modèle = chemin + contexte + paramètres GPU. Figé, non modifiable à chaud."""

    path: str
    n_ctx: int
    mmproj: str | None = (
        None  # projector visuel (SigLIP2/GLM-V) — requis pour VL et OCR
    )
    chat_handler: str | None = None  # chat_handler multimodal (ex: "ggml-vision-chat")
    n_gpu_layers: int = -1  # auto via HardwareDetector
    flash_attn: bool = True
    type_k: int = 8
    type_v: int = 8  # KV Q8_0
    n_keep: int = 512
    use_mmap: bool = True
    use_mlock: bool = False
    supports_vision: bool = False


MODEL_CONFIGS: dict[str, ModelConfig] = {
    "LFM2.5-8B": ModelConfig(
        path="models/LLM/LFM2.5-8B-A1B-UD-Q4_K_M.gguf",  # 5.0G Q4_K_M — présent
        # 128K natif (lfm2moe.context_length = 128000). Le KV ne porte que sur
        # 6 couches d'attention sur 24 — les 18 shortconv n'ont pas de KV cache :
        # 816 MiB mesurés à 131072, soit ~6,3 GiB avec le modèle et le compute.
        # D'où le seuil de 8 Go libres dans resolve_n_ctx.
        n_ctx=131072,
        supports_vision=False,  # texte pur uniquement (LLM_vs_VLM.md)
    ),
    # Brouillon : l'OCR passe par PaddleOCR-VL-1.6 (worker transformers persistant,
    # venv transformers 4.56 — jamais par llama.cpp). GLM-OCR
    # (GGUF Vision-document) n'a jamais été livré ; entrée supprimée, pas d'alias.
    "PADDLEOCR-VL-1.6": ModelConfig(
        path="models/PaddleOCR-VL-1.6",  # dossier safetensors (worker, pas GGUF)
        n_ctx=8192,  # indicatif (fenêtre de travail) ; le worker gère son propre batch
        supports_vision=True,
    ),
    "LFM2.5-VL-3B": ModelConfig(
        path="models/Vision-general/LFM2.5-VL-3B-UD-Q4_K_XL.gguf",  # 1.6G Q4_K_XL
        mmproj="models/Vision-general/mmproj-LFM2.5-VL-3B-BF16.gguf",  # 856 MB BF16 (recommandé)
        n_ctx=32768,  # 32K MAX, dense
        flash_attn=True,  # flash-attn ON pour VL (bon pour tiling dynamique 512×512)
        chat_handler="ggml-vision-chat",  # handler multimodal pour VL
        supports_vision=True,
    ),
}


def resolve_n_ctx(model_name: str, free_vram: int, estimated_tokens: int) -> int:
    """
    Calcule le n_ctx à utiliser selon la VRAM libre.
    - LFM2.5-8B : 128K alloué dès 8 Go libres (KV 816 MiB mesuré à 131072,
      ~6,3 GiB total) ; 32K entre 7 et 8 Go ; sinon plancher 8K remonté par
      l'estimation (plafond 32K). Le plancher de 8K est indispensable : le prompt
      fixe dépasse déjà 7K, donc une fenêtre dimensionnée sur le seul prompt
      rendrait le chat impossible.
    - VL-3B : toujours 32K
    - PADDLEOCR-VL-1.6 : ne passe JAMAIS par ici (worker transformers) → ValueError explicite
    """
    GB = 1024 * 1024 * 1024
    if model_name == "LFM2.5-8B":
        if free_vram >= 8 * GB:
            n_ctx = 131072
        elif free_vram >= 7 * GB:
            n_ctx = 32768
        else:
            n_ctx = min(32768, max(8192, estimated_tokens + 2048))
    elif model_name == "LFM2.5-VL-3B":
        n_ctx = 32768
    elif model_name == "PADDLEOCR-VL-1.6":
        raise ValueError(
            "PADDLEOCR-VL-1.6 ne passe pas par llama.cpp (worker transformers : backend/ingestion/paddle_client.py)"
        )
    else:
        raise ValueError(f"Modèle inconnu : {model_name}")
    return n_ctx


# Budgets Phase 1 — spec 01/03 : contrôle de boucle (16 appels, warn 14, dup 4/8, 4 erreurs)
MAX_ITER = 16
TOOL_TIMEOUT = 15
TOTAL_TIMEOUT = 300
MAX_RETRY = 1

# Borne d'indexation par artefact. Justification : un document dense de 20 pages
# fait ≈ 200 000 caractères, soit ≈ 100-200 fragments à ~2 000 caractères. 200
# laisse une marge (≈ 340 000 caractères à 15 % de recouvrement) sans borner un
# document courant ; une valeur plus basse doit rester un CHOIX EXPLICITE, jamais
# une perte silencieuse.
# `backend.search.chunks.MAX_CHUNKS` peut être surchargé par `THOT_MAX_CHUNKS`.
CHUNK_MAX_PAR_ARTEFACT = 200

# Contrôle de boucle orchestrateur (spec 01 § contrôle de boucle + 03 rappel)
MAX_TOOL_CALLS = 16  # plafond dur par tour utilisateur
MAX_SUCCESSFUL_TOOL_CALLS = 10
MAX_FAILING_CALLS_BEFORE_TOOL_BLOCK = 3
MAX_GENERIC_CONSECUTIVE_FAILURES = 3
WARN_TOOL_CALLS = 14  # injection message système « il te reste 3 appels… »
MAX_DUPLICATE_CALLS = 4  # même couple (outil, hash args) dans fenêtre 8 → sortie forcée
DUPLICATE_WINDOW = 8
MAX_CONSECUTIVE_ERRORS = 4  # erreurs d'outil consécutives → brief dégradé
WARN_MESSAGE = "Il te reste 3 appels avant le brief obligatoire — privilégie l'essentiel, ne cherche plus à compléter des détails"

# Barrière d'ingestion — orchestration.md §2.1 (tâche 5.1) + seuils §3.6 (à mesurer).
# Point unique des seuils d'attente : le code de boucle lit cet endroit, jamais un littéral recopié.
DELAI_BARRIERE_INGESTION = 120  # PROVISOIRE, arbitrage produit à mesurer — 15 s bloquerait prématurément les ingestions de gros PDF

# ── Limites d'ENTRÉE (Phase 7.2) : un seul point de vérité ──────────────────
# Toute taille d'entrée utilisateur (dépôt, sélection, aperçu, image, PDF,
# conteneur tableur) est décidée ICI. Les modules consomment ces valeurs au lieu
# de recopier des littéraux qui divergent — c'est exactement ce qui avait laissé
# 10 Mio côté Electron, 20 Mio côté backend et 5 Mio côté images.
#
# 20 Mio est la règle : un fichier utilisateur ne dépasse pas 20 Mio, qu'il
# arrive par le sélecteur, le glisser-déposer ou un upload de discussion. Le
# studio d'image garde 32 Mio, seule exception documentée : ses images de travail
# sont des PNG non compressés produits par l'éditeur, pas des entrées brutes.
#
# `ipc_line_bytes` n'est pas une taille de fichier : c'est la borne de la ligne
# JSON qui transporte un fichier en base64. 20 Mio de fichier font ~26,7 Mio de
# base64 ; 32 Mio laissent la marge du JSON sans autoriser une ligne sans fin.
# La lecture stdin est bornée AVANT accumulation (ipc._lire_ligne_bornee), donc
# cette constante est un plafond dur de mémoire, pas un simple contrôle a
# posteriori.
MIB = 1024 * 1024

INPUT_LIMITS: dict[str, int] = {
    # Fichiers utilisateur — 20 Mio partout (upload, sélecteur, PDF, image).
    "upload_bytes": 20 * MIB,
    "image_bytes": 20 * MIB,
    # Aperçu immédiat : même plafond que l'entrée. Il est transmis en base64 ;
    # la borne de ligne IPC ci-dessous doit donc le contenir.
    "preview_bytes": 20 * MIB,
    # Exception documentée : image de travail du studio (PNG éditeur).
    "image_studio_bytes": 32 * MIB,
    # Transport IPC (base64 compris).
    "ipc_line_bytes": 32 * MIB,
    "base64_bytes_max": 20 * MIB,
    # Conteneurs tableurs : inspection ZIP sans extraction.
    "zip_max_decompressed_bytes": 200 * MIB,
    "zip_max_ratio": 100,
    "zip_max_members": 10_000,
    "zip_max_depth": 16,
    # Matérialisation des feuilles (lignes, cellules, feuilles).
    "sheet_max_rows": 100_000,
    "sheet_max_cells": 2_000_000,
    "sheet_max_sheets": 64,
    # PDF : une seule borne de pages pour le gate et l'ingestion, et un plafond
    # de pixels pour refuser une MediaBox géante avant `Image.frombytes`.
    "pdf_max_pages": 20,
    "pdf_max_pixels": 40_000_000,
}

# Seuils outils — spec 01 § constantes + 02 contrats + 03 pipelines (point unique, jamais de littéral recopié).
TOOL_LIMITS: dict[str, int | float] = {
    # Recherche et index (LFM2.5-ColBERT-350M — contraintes dures modèle)
    "query_max_chars": 150,  # search_info queries[], find_files text — 32 tokens max, troncature silencieuse au-delà
    "chunk_tokens": 500,  # ingestion texte : 500 tokens (marge sous max_seq_length=511)
    "chunk_overlap_pct": 15,  # 10-20% recouvrement, médiane figée (ajustable)
    "queries_max": 3,  # search_info : 1-3 requêtes/appel (SEARCH_INFO_MAX_QUERIES)
    "results_limit": 5,  # RESULTS_LIMIT par requête (choix produit)
    "questions_max": 4,  # ask_files : 1-4 questions/appel
    "question_max_chars": 300,  # ask_files : chaque question ≤ 300 caractères
    "artifact_ids_max": 3,  # ask_files : 1-3 fichiers/appel
    "pairs_max": 6,  # ask_files : questions × fichiers ≤ 6 (non-paramétrable)
    "render_limit": 5,  # search_info/find_files : 5 max affichés (non-paramétrable)
    # Fichiers
    "ambiguity_gap": 0.05,  # find_files : écart top1-top2 < 0.05 → ambiguous
    "recent_days": 7,  # recency recent = 7 jours glissants
    "today_hours": 24,  # recency today = 24h glissantes
    "sheet_small_max_lines": 500,  # tableur ≤500 lignes + ≤5 feuilles → indexation complète
    "sheet_small_max_sheets": 5,  # (spec 03 § chunks d'index)
    "sheet_sample_chunks": 75,  # grand tableur : 50-100 chunks, médiane figée
    # Une seule borne de pages (Phase 7.4) : le gate et l'ingestion lisent la
    # même valeur. L'ancien couple 20/50 faisait refuser la 21ᵉ page par un
    # chemin et l'accepter par l'autre.
    "pdf_max_pages": INPUT_LIMITS["pdf_max_pages"],
    "pdf_sync_threshold": INPUT_LIMITS["pdf_max_pages"],
    "pdf_batch_threshold": 100,  # PROVISOIRE — idem, dépend du matériel cible réel
    # 200 ppp = valeur de rendu effectivement déployée à l'ingestion PDF (tâche 4.7) —
    # fiche mecanismes/ingestion/pdf.md §2.5 : « La résolution est un paramètre de
    # qualité et de mémoire à mesurer. Aucun nombre de DPI n'est adopté comme défaut
    # universel. » Sa cible finale relève d'un arbitrage produit (qualité OCR vs
    # mémoire/latence) à mesurer, comme les bornes de pages ci-dessus.
    "pdf_render_dpi": 200,  # PROVISOIRE — cible produit à mesurer (pdf.md §2.5)
    # Image et PDF (PP-DocLayoutV3 — spec 01/03)
    "layout_confidence_threshold": 0.3,  # dure par défaut, ajustable (défaut recette Paddle ; second défaut 0,5 dans PP-StructureV3)
    "layout_input_size": 800,  # dure — export ONNX 800×800 sans préservation du ratio
    "layout_batch_size": 8,  # ajustable — 8 latence / 64 débit
    # Brief
    "render_budget_tokens": 800,  # budget rendu texte outil (800-1000, médiane basse figée)
    # Image Phase 2 (C13 : service commun studio/chat, galerie = vue Artifact)
    "image_prompt_max_chars": 2000,  # generate_image prompt borné
    "image_references_max": 4,  # 0-4 références, profil à reproduire ; 5e refusée avant allocation
    # Recherche web Phase 4 (C15/C16/C18 — web_research)
    # Les quatre dernières lignes ne sont pas des réglages : ce sont des obligations de norme.
    "web_queries_max": 15,  # 15 requêtes max au total : 5 par web research, 3 web_research max, découpage auto en lots de 5
    "web_purpose_max_chars": 200,  # purpose : traçabilité de l'intention, obligatoire
    "web_hits_max": 5,  # résultats rendus par requête (render_limit réinvesti)
    "web_pages_max": 2,  # pages lues par appel — une page 10 kB ≈ 2 500 tokens
    "web_page_bytes_max": 524288,  # 512 KiB par page (décompression comprise)
    "web_bytes_max": 2097152,  # 2 MiB par appel, toutes pages confondues
    "web_url_max_chars": 250,  # longueur d'URL (sécurité)
    "web_redirects_max": 0,  # NORME : redirection non suivie, C18
    "web_timeout_s": 8,  # délai réseau par requête, sous TOOL_TIMEOUT 15 s
    "web_host_min_interval_s": 1,  # politesse : une requête sérialisée par hôte (MediaWiki : « in series »)
    "web_host_max": 2,  # 2 requêtes par hôte et par tour
    "web_turn_queries_max": 6,  # requêtes moteur par tour
    "web_conv_queries_max": 200,  # requêtes moteur par conversation
    "web_serp_ttl_s": 300,  # cache moteur global (convention observée)
    "web_page_ttl_s": 3600,  # cache page : Cache-Control sinon 10 % sinon 1 h (RFC 9111 §4.2.2)
    "web_robots_ttl_s": 86400,  # NORME RFC 9309 §2.4 : plafond de conservation 24 h
    "web_robots_max_bytes": 524288,  # NORME RFC 9309 §2.5 : plancher de parsing 500 KiB
    "web_snip_max_chars": 400,  # extrait rendu par source dans le résultat d'outil
}

# Étape B — description différée des figures d'un document. Le vocabulaire de
# claim est celui du ledger mémoire (TTL 5 min, heartbeat 60 s) : deux files du
# même dépôt ne doivent pas inventer deux durées de vie. `max_attempts` compte
# les EXÉCUTIONS RÉELLES, jamais les différés (chat/texte prioritaire, GPU
# occupé) : trois passages bloqués ne condamnent pas une figure jamais décrite.
FIGURES_LIMITS: dict[str, object] = {
    "claim_ttl_s": 300,
    "heartbeat_s": 60,
    "max_attempts": 3,
    "backoff_minutes": (1, 5, 15),
    "sweep_interval_s": 60,
    "debounce_s": 1.0,
    "defer_seconds": 60,
}

# Configuration obligatoire PP-DocLayoutV3 (spec 01/03 : non un seuil, sans elle troncature silencieuse à 10 pages)
PDF_LAYOUT_MAX_PAGES_CONFIG: dict[str, object] = {"max_num_input_imgs": None}

MAX_TOOL_CALLS_BY_TOOL = {
    "search_info": 4,
    "find_files": 4,
    "ask_files": 4,
    "web_research": 3,
    "doc_ocr": 3,
    "read_text": 3,
    "vision": 3,
    "generate_image": 1,
}
