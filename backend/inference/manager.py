"""
ModelManager — gestionnaire de modèles en chargement séquentiel strict
Notes.md § Gestion mémoire + § Double historique + § n_ctx adaptatif

But : ne jamais avoir 2 modèles en VRAM en même temps (borne 7.5 GB garantie).
- Un seul modèle résident à la fois (sauf LFM2.5-8B gardé chaud entre requêtes)
- Chargement paresseux (lazy) pour GLM-OCR et VL-3B (uniquement sur tool_call)
- Déchargement aggressif : del + gc.collect() + cuda.empty_cache()
- Contexte adaptatif : 128K si ≥12GB VRAM, sinon 32K
- Support multimodal : mmproj + chat_handler par modele
"""
import gc
import logging
from contextlib import contextmanager
from typing import Optional, Generator

logger = logging.getLogger(__name__)

try:
    from llama_cpp import Llama
except ImportError:
    Llama = None  # type: ignore

from backend.config.defaults import MODEL_CONFIGS, resolve_n_ctx
from backend.inference.hardware import HardwareDetector, estimate_layers

GB = 1024 * 1024 * 1024

# Helpers fusion v0.8 llm/streaming.py + prompt_cache.py (Phase 1 simplifie)
# streaming : parse chunk deja gere via llama_cpp delta ; on garde helper minimal
def parse_stream_chunk(chunk) -> dict:
    """Parse un chunk streaming llama_cpp (compat v0.8 streaming.py)."""
    if isinstance(chunk, bytes):
        chunk = chunk.decode("utf-8", errors="ignore")
    if isinstance(chunk, str):
        import json as _json

        try:
            return _json.loads(chunk)
        except Exception:
            return {}
    return chunk if isinstance(chunk, dict) else {}


def detect_finish_reason(payload: dict) -> str | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("done") is True:
        return payload.get("done_reason") or "done"
    reason = payload.get("finish_reason")
    return reason if isinstance(reason, str) and reason else None


# prompt_cache Phase 1 : n_keep=512 dans ModelConfig, pas de provider anthropic
def inject_cache_markers(messages: list[dict], system: str) -> tuple[list[dict], str]:
    """Phase 1 : prompt caching via n_keep llama_cpp, pas de cache_control externe."""
    return messages, system


class ModelManager:
    """Gardien des modeles : charge/decharge en sequentiel strict."""

    def __init__(self):
        self._current_model: Optional[object] = None  # modele actuellement en VRAM
        self._current_name: Optional[str] = None      # nom du modele charge
        self.detector = HardwareDetector()             # detecte le GPU au demarrage
        self.backend = self.detector.detect()          # config backend (cuda/vulkan/cpu...)

    @contextmanager
    def use_model(self, name: str, estimated_tokens: int = 4096, has_image: bool = False) -> Generator:
        """
        Context manager : `with manager.use_model("LFM2.5-8B") as llm:`
        - Si le bon modele est deja charge → on le reutilise direct (evite 500ms de reload)
        - Sinon → on decharge l'ancien puis on charge le nouveau
        - Garde-fou supports_vision : 8B texte pur ne doit jamais recevoir file:// image (ticket #23852)
        """
        if self._current_name == name and self._current_model is not None:
            yield self._current_model  # deja charge, on le rend
            return

        if self._current_model is not None:
            self._unload_current()  # libere la VRAM avant de charger le suivant

        model = self._load_model(name, estimated_tokens, has_image)
        self._current_model = model
        self._current_name = name
        yield model
        # on garde le modele chaud pour la prochaine requete (pas de dechargement auto)

    def _build_llm_kwargs(self, cfg, n_ctx: int, flash_attn: bool, mmproj: bool = True, extra_layers: int | None = None) -> dict:
        """Construit kwargs Llama selon config + flash_attn + mmproj."""
        kwargs: dict = {
            "model_path": cfg.path,
            "n_ctx": n_ctx,
            "n_gpu_layers": extra_layers if extra_layers is not None else cfg.n_gpu_layers,
            "flash_attn": flash_attn,
            "use_mmap": cfg.use_mmap,
            "use_mlock": cfg.use_mlock,
            "type_k": cfg.type_k,
            "type_v": cfg.type_v,
            "n_keep": cfg.n_keep,
            "verbose": False,
        }
        if cfg.supports_vision:
            if not cfg.mmproj:
                raise RuntimeError(f"{cfg.path} requires a multimodal projector (mmproj), but none is configured.")
            if mmproj:
                from pathlib import Path as _P
                if not _P(cfg.mmproj).exists():
                    raise RuntimeError(f"mmproj introuvable: {cfg.mmproj} (GGUF {cfg.path})")
                kwargs["mmproj"] = cfg.mmproj
                logger.info(f"  mmproj charge: {cfg.mmproj}")
            if not cfg.chat_handler:
                raise RuntimeError(f"{cfg.path} is multimodal but no chat_handler configured.")
            try:
                from llama_cpp.llama_chat_handler import AutoChatHandler
                handler = AutoChatHandler.from_model_path(cfg.path, chat_handler_name=cfg.chat_handler)
                if handler is None:
                    raise RuntimeError(f"chat_handler {cfg.chat_handler} a retourne None pour {cfg.path}")
                kwargs["chat_handler"] = handler
                logger.info(f"  chat_handler: {cfg.chat_handler}")
            except Exception as e:
                raise RuntimeError(f"chat_handler {cfg.chat_handler} non charge pour {cfg.path}: {e}") from e
        else:
            if mmproj and cfg.mmproj:
                kwargs["mmproj"] = cfg.mmproj
                logger.info(f"  mmproj charge: {cfg.mmproj}")
            if cfg.chat_handler:
                try:
                    from llama_cpp.llama_chat_handler import AutoChatHandler
                    handler = AutoChatHandler.from_model_path(cfg.path, chat_handler_name=cfg.chat_handler)
                    kwargs["chat_handler"] = handler
                    logger.info(f"  chat_handler: {cfg.chat_handler}")
                except Exception as e:
                    logger.warning(f"chat_handler {cfg.chat_handler} non charge: {e}")
        return kwargs

    def _load_model(self, name: str, estimated_tokens: int, has_image: bool = False):
        """Charge un modele depuis le disque avec les bons parametres (mmproj + chat_handler + flash_attn par modele)."""
        if Llama is None:
            raise RuntimeError("llama_cpp non installe — lance : pip install llama-cpp-python")

        cfg = MODEL_CONFIGS[name]

        # Garde-fou vision : 8B texte pur ne doit jamais recevoir d'image (ticket #23852)
        if not cfg.supports_vision and has_image:
            raise ValueError(f"[supports_vision] {name} texte pur ne supporte pas vision (supports_vision=False) — utilisez VL-3B/GLM-OCR. Ticket #23852")

        # n_ctx adaptatif : 128K si ≥12GB VRAM, sinon 32K (evite OOM 11.5GB sur 8GB)
        n_ctx = resolve_n_ctx(name, self.backend.free_vram, estimated_tokens)
        logger.info(f"Chargement {name}: n_ctx={n_ctx}, flash_attn={cfg.flash_attn}, mmproj={cfg.mmproj}")

        errors: list[tuple[str, Exception]] = []

        # Tentative 1 : config ideelle (flash_attn figure + mmproj/chat_handler)
        try:
            kwargs = self._build_llm_kwargs(cfg, n_ctx, cfg.flash_attn, mmproj=True)
            llm = Llama(**kwargs)
            logger.info(f"{name} charge avec succes (config ideelle)")
            return llm
        except Exception as e:
            errors.append(("config ideelle", e))
            logger.error(f"Tentative 1 echouee pour {name}: {e}")

        # Tentative 2 (OOM) : partial GPU layers sans mmproj
        if any("out of memory" in str(e).lower() or "CUDA" in str(e) for _, e in errors):
            try:
                layer_count = 24  # approximation LFM2.5-8B
                partial = estimate_layers(self.backend.free_vram, 5 * GB, 1 * GB, layer_count)
                kwargs = self._build_llm_kwargs(cfg, n_ctx, False, mmproj=False, extra_layers=partial)
                llm = Llama(**kwargs)
                logger.info(f"{name} charge en fallback OOM (partial layers={partial})")
                return llm
            except Exception as e2:
                errors.append(("fallback OOM", e2))
                logger.error(f"Tentative 2 echouee pour {name}: {e2}")

        # Tentative 3 : flash_attn desactive + mmproj/chat_handler gardes
        try:
            kwargs = self._build_llm_kwargs(cfg, n_ctx, False, mmproj=True)
            llm = Llama(**kwargs)
            logger.info(f"{name} charge avec flash_attn=False")
            return llm
        except Exception as e:
            errors.append(("flash_attn=False", e))
            logger.error(f"Tentative 3 echouee pour {name}: {e}")

        # Tentative 4 : sans mmproj/chat_handler (si handler provoque l'echec)
        try:
            kwargs = self._build_llm_kwargs(cfg, n_ctx, False, mmproj=False)
            llm = Llama(**kwargs)
            logger.warning(f"{name} charge sans mmproj/chat_handler (images seront ignorees!)")
            return llm
        except Exception as e:
            errors.append(("sans mmproj", e))
            logger.error(f"Tentative 4 echouee pour {name}: {e}")

        raise RuntimeError(f"Impossible de charger {name} apres {len(errors)} tentatives: {errors}") from errors[-1][1]

    def _unload_current(self):
        """Decharge le modele actuel et libere la VRAM/RAM."""
        if self._current_model is not None:
            try:
                del self._current_model  # supprime la reference Python
            except Exception:
                pass
            self._current_model = None
            self._current_name = None
            gc.collect()  # force le ramasse-miettes a liberer la RAM
            try:
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()  # vide le cache CUDA (VRAM)
            except ImportError:
                pass  # torch non installe, on ignore

    def unload_all(self):
        """Decharge tout (appele a la fermeture de l'app)."""
        self._unload_current()
