"""
HardwareDetector — Cascade GPU pour llama-cpp-python
Notes.md § Inférence IPC Python — Cascade GPU (validé 30/08)

But : détecter le GPU disponible et choisir le backend optimal.
- Ordre par vendor, pas global : NVIDIA → CUDA, AMD → ROCm, Apple → Metal, sinon Vulkan puis CPU
- Vérification que le wheel llama-cpp a bien le support GPU (sinon fallback CPU silencieux)
- Formule simplifiée pour calculer combien de couches (layers) tiennent en VRAM

LA DIFFÉRENCE ENTRE « PAS DE CARTE » ET « PAS D'OUTIL ». Avant, `_run` renvoyait
`None` dans les deux cas : `nvidia-smi` absent du PATH et `nvidia-smi` installé
mais muet étaient le même état, un `None` que la cascade lisait comme « NVIDIA
invisible » puis comme « CPU suffit ». Cette confusion a un prix précis : sans
outil, aucune VRAM n'est MESURÉE, et les portes mémoire du budget 8 Go (voir
`memprof.RunRecord.gates_ok`) se calculent sur des zéros — donc passent. Un budget
qui ne peut pas échouer n'est pas un budget. Les deux événements sont donc
nommés séparément : `BackendConfig.dependances_absentes` porte l'outil manquant,
et `paths.exiger_nvidia_smi()` lève pour qui a besoin de VRAIMENT mesurer.
"""
import subprocess
from dataclasses import dataclass, replace
from typing import Optional

from backend.config.paths import (
    NOM_NVIDIA_SMI,
    VARIABLE_NVIDIA_SMI,
    EMPLACEMENTS_NVIDIA_SMI,
    CHEMINS_NVIDIA_SMI,
    DependanceIntrouvable,
    resoudre_outil_systeme,
)

MB = 1024 * 1024
GB = 1024 * MB


@dataclass(frozen=True)
class BackendConfig:
    backend: str  # "cuda" | "rocm" | "metal" | "vulkan" | "cpu"
    gpu_name: str
    total_vram: int  # bytes
    free_vram: int   # bytes
    n_gpu_layers: int  # -1 = all, 0 = cpu
    # OUTILS DE MESURE ABSENTS, pas nécessairement des cartes absentes. On ne les
    # invente pas : la cascade les a essayés dans l'ordre et les a trouvés
    # introuvables. Le champ est ici parce que le produit doit pouvoir dire
    # « carte présente, VRAM non mesurable » au lieu de choisir le CPU en silence.
    dependances_absentes: tuple[str, ...] = ()


def _run(cmd: list[str]) -> Optional[str]:
    """Exécute une commande système et retourne sa sortie texte, ou None si échouée.

    La signature ne change pas — plusieurs tests la patchent pour ne dépendre
    d'aucun GPU réel — mais la question « est-elle installée ? » a désormais une
    réponse séparée : voir `outil_present`.
    """
    if not outil_present(cmd[0]):
        return None
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            return r.stdout
    except Exception:
        pass
    return None


def outil_present(nom: str) -> bool:
    """L'outil de mesure est-il résolvable sur cette machine ?

    Un outil de PILOTE est la seule dépendance d'exécution que l'application ne
    peut ni embarquer ni transporter : il appartient au constructeur de la carte.
    Le résolveur le cherche donc aussi dans les emplacements que le pilote
    garantit, et son échec reste une question de MACHINE, pas d'organisation de
    dossiers.

    `THOT_NVIDIA_SMI` ne sert que pour `nvidia-smi` : la nommer pour `rocminfo`
    permettrait de faire mesurer le pilote NVIDIA par le mauvais binaire, ce qui est
    pire que de ne rien trouver.
    """
    est_nvidia = nom == NOM_NVIDIA_SMI
    try:
        resoudre_outil_systeme(
            nom,
            variable=VARIABLE_NVIDIA_SMI if est_nvidia else None,
            emplacements=EMPLACEMENTS_NVIDIA_SMI if est_nvidia else (),
            chemins_connus=CHEMINS_NVIDIA_SMI if est_nvidia else (),
        )
    except DependanceIntrouvable:
        return False
    return True


def _nvidia_smi() -> Optional[BackendConfig]:
    """Détecte un GPU NVIDIA via nvidia-smi (CUDA).

    Renvoie `None` si l'outil est absent : ce n'est pas la même chose qu'un GPU
    NVIDIA inexistant, et `detect()` se charge de nommer la différence.
    """
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


# La cascade, et pour chaque sonde l'OUTIL dont elle dépend. L'outil se résout
# AVANT la sonde : c'est lui qui distingue « la carte manque » de « la mesure est
# impossible », et donc ce qui permet de nommer la conséquence au lieu de la
# laisser se déduire d'un `None`.
CASCADE = (
    (_nvidia_smi, NOM_NVIDIA_SMI),
    (_rocm_info, "rocminfo"),
    (_metal_info, "system_profiler"),
    (_vulkan_info, "vulkaninfo"),
)


class HardwareDetector:
    """Détecteur qui teste chaque backend dans l'ordre de préférence."""

    def detect(self) -> BackendConfig:
        """Détecte le backend, en nommant les outils de mesure introuvables.

        L'ordre est inchangé — NVIDIA → AMD → Apple → Vulkan → CPU — mais le
        résultat porte maintenant la liste des outils qui MANQUAIENT au moment de
        la détection. C'est la seule façon, pour l'appelant, de distinguer
        « cette machine n'a pas de GPU » (légitime, le CPU suit) de « cette
        machine n'a pas d'outil de mesure » (le CPU suit en croyant mesurer), et
        la Bible refuse le second en silence.
        """
        absences: list[str] = []
        for sonde, outil in CASCADE:
            if not outil_present(outil):
                absences.append(outil)
            cfg = sonde()
            if cfg:
                return replace(cfg, dependances_absentes=tuple(absences))
        # Aucun GPU trouvé → inférence 100% CPU
        return BackendConfig("cpu", "CPU", 0, 0, 0, tuple(absences))

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
