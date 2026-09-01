"""
ModelManager — gestionnaire de modèles en chargement séquentiel strict
Notes.md § Gestion mémoire + § Double historique + § n_ctx adaptatif

But : ne jamais avoir 2 modèles en VRAM en même temps (borne 7.5 GB garantie).
- Un seul modèle résident à la fois (sauf LFM2.5-8B gardé chaud entre requêtes)
- Chargement paresseux (lazy) pour GLM-OCR et VL-3B (uniquement sur tool_call)
- Déchargement explicite : close + del handler + gc + wait VRAM (llama.cpp n'utilise pas torch)
- Contexte adaptatif : 128K si ≥12GB VRAM, sinon 32K
- Support multimodal : mmproj + chat_handler par modele
"""
import gc
import logging
import time
import subprocess
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
        self._current_chat_handler: Optional[object] = None  # handler multimodal (CLIP) à libérer avec le modèle
        self._current_name: Optional[str] = None      # nom du modele charge
        self.detector = HardwareDetector()             # detecte le GPU au demarrage
        self.backend = self.detector.detect()          # config backend (cuda/vulkan/cpu...)
        self._min_required_vram_mb = 6000  # seuil pour rechargement 8B (~7G) — ajustable

    def _get_free_vram_mb(self) -> int:
        """VRAM libre via nvidia-smi (fallback) — évite torch.cuda qui ne voit pas llama.cpp."""
        try:
            out = subprocess.check_output(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"], text=True, timeout=1)
            return int(out.strip().split("\n")[0])
        except Exception:
            pass
        try:
                import pynvml
                pynvml.nvmlInit()
                handle = pynvml.nvmlDeviceGetHandleByIndex(0)
                info = pynvml.nvmlDeviceGetMemoryInfo(handle)
                return int(info.free // (1024*1024))
        except Exception:
            pass
        # Fallback : backend detector free_vram
        try:
            return int(self.backend.free_vram // (1024*1024))
        except Exception:
            return 0

    def _wait_for_vram_release(self, timeout_s: float = 3.0) -> None:
        start = time.monotonic()
        while time.monotonic() - start < timeout_s:
            free = self._get_free_vram_mb()
            if free >= self._min_required_vram_mb:
                return
            time.sleep(0.1)
        logger.warning("VRAM non libérée après déchargement (%s Mo libres, %s requis)", self._get_free_vram_mb(), self._min_required_vram_mb)

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
            logger.info("VRAM avant unload %s: %s Mo libres", self._current_name, self._get_free_vram_mb())
            self._unload_current()  # libere la VRAM avant de charger le suivant
            logger.info("VRAM après unload: %s Mo libres", self._get_free_vram_mb())

        model = self._load_model(name, estimated_tokens, has_image)
        self._current_model = model
        # handler déjà stocké via _build_llm_kwargs side-effect si vision
        self._current_name = name
        yield model
        # on garde le modele chaud pour la prochaine requete (pas de dechargement auto)

    def _build_llm_kwargs(self, cfg, n_ctx: int, flash_attn: bool, mmproj: bool = True, extra_layers: int | None = None) -> dict:
        """Construit kwargs Llama selon config + flash_attn + mmproj (corrige llama_chat_handler -> llama_chat_format)."""
        # Vérif en amont : module correct existe
        try:
            import llama_cpp.llama_chat_format  # noqa: F401
        except ImportError as exc:
            raise RuntimeError("llama_cpp.llama_chat_format introuvable — verifiez version llama-cpp-python") from exc

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
            # Mapping handler config -> classe réelle (0.3.34 n'a pas LFM2VL, fallback documenté)
            handler = self._build_vision_handler(cfg, cfg.mmproj if mmproj else None)
            kwargs["chat_handler"] = handler
            self._current_chat_handler = handler
            logger.info(f"  chat_handler: {cfg.chat_handler} -> {handler.__class__.__name__}")
        else:
            self._current_chat_handler = None
            if mmproj and cfg.mmproj:
                kwargs["mmproj"] = cfg.mmproj
                logger.info(f"  mmproj charge: {cfg.mmproj}")
            if cfg.chat_handler:
                try:
                    handler = self._build_vision_handler(cfg, cfg.mmproj)
                    kwargs["chat_handler"] = handler
                    self._current_chat_handler = handler
                    logger.info(f"  chat_handler: {cfg.chat_handler}")
                except Exception as e:
                    logger.warning(f"chat_handler {cfg.chat_handler} non charge: {e}")
                    self._current_chat_handler = None
        return kwargs

    def _build_vision_handler(self, cfg, mmproj_path: str | None):
        """Instancie le bon handler multimodal (LFM2VL > Qwen25VL > Llava)."""
        if not mmproj_path:
            raise RuntimeError(f"mmproj_path requis pour {cfg.path}")
        import llama_cpp.llama_chat_format as fmt
        # Priorité Réponses.md : LFM2VLChatHandler si disponible
        candidates = []
        # Mapping demandé spec : ggml-vision-chat -> LFM2VL, ggml-ocr-chat -> Qwen/GLM
        if cfg.chat_handler == "ggml-vision-chat":
            candidates = ["LFM2VLChatHandler", "Qwen25VLChatHandler", "Llava15ChatHandler", "Llava16ChatHandler", "MTMDChatHandler"]
        elif cfg.chat_handler == "ggml-ocr-chat":
            candidates = ["Qwen25VLChatHandler", "Llava15ChatHandler", "MTMDChatHandler"]
        else:
            candidates = [cfg.chat_handler]

        last_err = None
        for cls_name in candidates:
            cls = getattr(fmt, cls_name, None)
            if cls is None:
                continue
            try:
                # La plupart prennent clip_model_path=mmproj
                try:
                    return cls(clip_model_path=mmproj_path)
                except TypeError:
                    return cls(mmproj_path)
            except Exception as e:
                last_err = e
                logger.warning(f"Handler {cls_name} echoue: {e}")
                continue
        raise RuntimeError(f"Aucun handler multimodal compatible pour {cfg.path} (tentes {candidates}): {last_err}")

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

        # Stratégie retry distingue OOM vs config (Réponses.md n°9)
        LOAD_STRATEGIES = [
            ("nominal", {"flash": cfg.flash_attn, "mmproj": True, "delay": 0}),
            ("apres_gc_force", {"flash": cfg.flash_attn, "mmproj": True, "delay": 0.5}),
            ("flash_off", {"flash": False, "mmproj": True, "delay": 0}),
            ("sans_mmproj", {"flash": False, "mmproj": False, "delay": 0}),
        ]

        for strat_name, opts in LOAD_STRATEGIES:
            if opts["delay"]:
                gc.collect()
                time.sleep(opts["delay"])
                self._wait_for_vram_release(timeout_s=2.0)
            # Si OOM déjà détecté, tente directement n_gpu_layers réduit
            if any("out of memory" in str(e).lower() or "CUDA" in str(e) for _, e in errors):
                try:
                    partial = estimate_layers(self.backend.free_vram, 5 * GB, 1 * GB, 24)
                    kwargs = self._build_llm_kwargs(cfg, n_ctx, False, mmproj=False, extra_layers=partial)
                    llm = Llama(**kwargs)
                    logger.info(f"{name} charge en fallback OOM ({strat_name}, partial={partial})")
                    return llm
                except Exception as e:
                    errors.append((f"{strat_name}_oom_partial", e))
                    logger.error(f"Tentative {strat_name} OOM échouée: {e}")
                    continue
            try:
                kwargs = self._build_llm_kwargs(cfg, n_ctx, opts["flash"], mmproj=opts["mmproj"])
                llm = Llama(**kwargs)
                logger.info(f"{name} charge avec succes ({strat_name})")
                return llm
            except Exception as e:
                errors.append((strat_name, e))
                logger.error(f"Tentative {strat_name} echouee pour {name}: {e}")
                # Si erreur n'est pas OOM, continue vers flash_off etc.
                continue

        raise RuntimeError(f"Impossible de charger {name} apres {len(errors)} tentatives: {errors}") from errors[-1][1]

    def _unload_current(self):
        """Decharge le modele actuel et libere la VRAM/RAM — ferme handler multimodal explicitement."""
        if self._current_model is None and self._current_chat_handler is None:
            return
        model = self._current_model
        handler = self._current_chat_handler
        # Nullifie d'abord pour éviter référence résiduelle dans le manager
        self._current_model = None
        self._current_chat_handler = None
        self._current_name = None
        if model is not None:
            try:
                if hasattr(model, "close"):
                    model.close()
                else:
                    del model
            except Exception as e:
                logger.warning(f"close model échoué: {e}")
                try:
                    del model
                except Exception:
                    pass
        if handler is not None:
            try:
                del handler
            except Exception:
                pass
        gc.collect()
        self._wait_for_vram_release(timeout_s=3.0)
        # torch vide uniquement si dispo — ne libère pas llama.cpp mais harmless
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass

    def unload_all(self):
        """Decharge tout (appele a la fermeture de l'app)."""
        self._unload_current()
