# Configuration figée Phase 1 — aucun .env, aucun BaseSettings
# Tout est hardcodé en frozen dataclass (zéro surface d'attaque config)
from dataclasses import dataclass

@dataclass(frozen=True)
class ModelConfig:
    """Un modèle = chemin + contexte + paramètres GPU. Figé, non modifiable à chaud."""
    path: str
    n_ctx: int
    mmproj: str | None = None  # projector visuel (SigLIP2/GLM-V) — requis pour VL et OCR
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
        n_ctx=131072,  # 128K natif, mais adaptatif 32K si 8GB (voir resolve_n_ctx)
        supports_vision=False,  # texte pur uniquement (LLM_vs_VLM.md)
    ),
    "GLM-OCR": ModelConfig(
        path="models/Vision-document/GLM-OCR-f16.gguf",  # 1.7G f16
        mmproj="models/Vision-document/mmproj-GLM-OCR-Q8_0.gguf",  # 484 MB
        n_ctx=8192,  # 8K suffit pour OCR
        flash_attn=False,  # GLM-OCR: flash-attn OFF (#19721) — pas de fallback, paramètre fixe
        chat_handler="ggml-ocr-chat",  # handler multimodal pour OCR
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
    - LFM2.5-8B : 128K natif si ≥12GB, sinon adaptatif 4K-32K (évite OOM 11.5GB sur 8GB)
    - VL-3B : toujours 32K
    - GLM-OCR : toujours 8K
    """
    GB = 1024 * 1024 * 1024
    if model_name == "LFM2.5-8B":
        n_ctx = 131072 if free_vram >= 12 * GB else min(32768, max(4096, estimated_tokens + 1024))
    elif model_name == "LFM2.5-VL-3B":
        n_ctx = 32768
    elif model_name == "GLM-OCR":
        n_ctx = 8192
    else:
        raise ValueError(f"Modèle inconnu : {model_name}")
    return n_ctx

# Budgets Phase 1
MAX_ITER = 5
TOOL_TIMEOUT = 15
TOTAL_TIMEOUT = 300
