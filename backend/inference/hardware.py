"""
HardwareDetector — Cascade GPU pour llama-cpp-python
Notes.md § Inférence IPC Python — Cascade GPU (validé 30/08)

But : détecter le GPU disponible et choisir le backend optimal.
- Ordre par vendor, pas global : NVIDIA → CUDA, AMD → ROCm, Apple → Metal, sinon Vulkan puis CPU
- Vérification que le wheel llama-cpp a bien le support GPU (sinon fallback CPU silencieux)
- Formule simplifiée pour calculer combien de couches (layers) tiennent en VRAM
"""
import subprocess
import shutil
from dataclasses import dataclass
from typing import Optional

MB = 1024 * 1024
GB = 1024 * MB

@dataclass(frozen=True)
class BackendConfig:
    backend: str  # "cuda" | "rocm" | "metal" | "vulkan" | "cpu"
    gpu_name: str
    total_vram: int  # bytes
    free_vram: int   # bytes
    n_gpu_layers: int  # -1 = all, 0 = cpu


def _run(cmd: list[str]) -> Optional[str]:
    """Exécute une commande système et retourne sa sortie texte, ou None si échouée."""
    if not shutil.which(cmd[0]):  # commande non installée sur ce système
        return None
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            return r.stdout
    except Exception:
        pass
    return None


def _nvidia_smi() -> Optional[BackendConfig]:
    """Détecte un GPU NVIDIA via nvidia-smi (CUDA)."""
    out = _run(["nvidia-smi", "--query-gpu=name,memory.total,memory.free", "--format=csv,noheader,nounits"])
    if not out:
        return None
    # Exemple de sortie : "NVIDIA GeForce RTX 4070 SUPER, 12282, 11500"
    try:
        name, total, free = [s.strip() for s in out.strip().split(",")]
        total_b = int(total) * MB  # total VRAM du GPU
        free_b = int(free) * MB    # VRAM libre actuellement
        return BackendConfig("cuda", name, total_b, free_b, -1)  # -1 = tout décharger sur GPU si possible
    except Exception:
        return None


def _rocm_info() -> Optional[BackendConfig]:
    """Détecte un GPU AMD via rocminfo (ROCm)."""
    out = _run(["rocminfo"])
    if out and "GPU" in out:
        return BackendConfig("rocm", "AMD ROCm", 8*GB, 6*GB, -1)  # valeurs estimées, à affiner si besoin
    return None


def _metal_info() -> Optional[BackendConfig]:
    """Détecte Apple Silicon via system_profiler (Metal)."""
    out = _run(["system_profiler", "SPDisplaysDataType"])
    if out and "Metal" in out:
        # Sur Mac, mémoire unifiée : le GPU partage la RAM système
        return BackendConfig("metal", "Apple Silicon", 16*GB, 12*GB, -1)
    return None


def _vulkan_info() -> Optional[BackendConfig]:
    """Détecte un GPU via vulkaninfo (fallback universel NVIDIA/AMD/Intel)."""
    out = _run(["vulkaninfo"])
    if out and "GPU" in out:
        return BackendConfig("vulkan", "Vulkan", 8*GB, 6*GB, -1)
    return None


class HardwareDetector:
    """Détecteur qui teste chaque backend dans l'ordre de préférence."""

    def detect(self) -> BackendConfig:
        # Ordre : NVIDIA → AMD → Apple → Vulkan → CPU (fallback)
        for fn in [_nvidia_smi, _rocm_info, _metal_info, _vulkan_info]:
            cfg = fn()
            if cfg:
                return cfg
        # Aucun GPU trouvé → inférence 100% CPU
        return BackendConfig("cpu", "CPU", 0, 0, 0)

    def supports_gpu_offload(self) -> bool:
        """Vérifie que le wheel llama-cpp a bien été compilé avec le support GPU."""
        try:
            from llama_cpp import llama_supports_gpu_offload
            return llama_supports_gpu_offload()  # True = wheel GPU ok, False = wheel CPU seul (cache pollué)
        except ImportError:
            return False  # llama_cpp non installé


def estimate_layers(free_vram: int, model_size: int, kv_size: int, layers_total: int) -> int:
    """
    Estime combien de couches (layers) du modèle tiennent en VRAM.
    Formule simplifiée Notes.md § Cascade #4 (robuste pour 1 modèle résident).
    - free_vram : VRAM libre détectée
    - model_size : taille des poids du modèle
    - kv_size : taille du KV cache (dépend de n_ctx)
    - layers_total : nombre total de couches du modèle
    """
    overhead = 500 * MB  # marge pour le graphe de calcul + overhead système
    available = free_vram - model_size - kv_size - overhead
    if available <= 0:
        return 0  # pas assez de place, tout sur CPU
    per_layer = model_size // layers_total  # taille moyenne d'une couche
    return min(layers_total, max(0, available // per_layer))
