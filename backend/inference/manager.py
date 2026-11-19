"""
ModelManager — gestionnaire de modèles en chargement séquentiel strict
Notes.md § Gestion mémoire + § Double historique + § n_ctx adaptatif

But : ne jamais avoir 2 modèles en VRAM en même temps (borne 7.5 GB garantie).
- Un seul modèle résident à la fois (sauf LFM2.5-8B gardé chaud entre requêtes)
- Chargement paresseux (lazy) pour VL-3B (uniquement sur tool_call ; COMME AVANT :
  aussi GLM-OCR — brouillon : OCR = worker PaddleOCR-VL hors manager)
- Déchargement explicite : close + del handler + gc + wait VRAM (llama.cpp n'utilise pas torch)
- Contexte adaptatif : 128K si ≥12GB VRAM, sinon 32K
- Support multimodal : mmproj + chat_handler par modele
"""

import gc
import logging
import threading
import time
import subprocess
from contextlib import contextmanager
from typing import Optional, Generator

logger = logging.getLogger(__name__)

try:
    from llama_cpp import Llama
except (
    Exception
) as exc:  # ImportError, OSError (missing libs), RuntimeError (shared lib)
    Llama = None  # type: ignore
    logger.warning("llama_cpp non importable (inference désactivée) : %s", exc)
    print(f"[manager] WARNING: llama_cpp import failed: {exc}", flush=True)

from backend.config.defaults import MODEL_CONFIGS, resolve_n_ctx
from backend.config.paths import DependanceSystemeAbsente, exiger_nvidia_smi
from backend.inference.hardware import HardwareDetector, estimate_layers

GB = 1024 * 1024 * 1024

# Marge entre l'estimation de prompt et la fenêtre : couvre le template de chat et
# l'imprécision du ratio caractères/token. En dessous de cette place, recharger le
# modèle pour une fenêtre plus grande a un sens ; au-dessus, la réutilisation suffit
# et `LlamaCppRuntime` plafonne la sortie.
REUSE_CONTEXT_MARGIN = 512
# Note : cette marge (512) est volontairement DISTINCTE de
# `CONTEXT_SAFETY_TOKENS` (256) de `backend/inference/runtime.py`. On n'importe pas
# celle-ci ici : `runtime` dépend déjà indirectement de `manager`, et la marge de
# réutilisation (prompt vs fenêtre allouée) n'a pas la même raison d'être que la
# marge de sortie (tokens réservés à la réponse). Deux constantes nommées valent
# mieux qu'un couplage circulaire.


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
    """Gardien des modeles : charge/decharge en sequentiel strict + FSM C07."""

    def __init__(self):
        self._current_model: Optional[object] = None  # modele actuellement en VRAM
        self._current_chat_handler: Optional[object] = (
            None  # handler multimodal (CLIP) à libérer avec le modèle
        )
        self._current_name: Optional[str] = None  # nom du modele charge
        self._current_n_ctx: Optional[int] = None  # contexte réellement alloué
        self.detector = HardwareDetector()  # detecte le GPU au demarrage
        self.backend = self.detector.detect()  # config backend (cuda/vulkan/cpu...)
        self._min_required_vram_mb = (
            6000  # seuil pour rechargement 8B (~7G) — ajustable
        )
        # FSM C07 : unloaded, loading, ready, running, unloading, failed (pseudocode/backend_socle.md)
        self._state: str = "unloaded"
        self._generation: int = 0  # génération handle, invalide après unload (C07)
        self._owner: str | None = None
        self._state_lock = threading.Lock()
        self._admission = threading.Semaphore(
            1
        )  # 1 run lourd actif pour profil 8 Go (C07)
        self._admission_owner: str | None = None
        # Outil de mesure de la VRAM : `None` = pas encore résolu, `""` = absent
        # (résolu une fois, car la résolution touche le disque et le PATH).
        self._outil_vram_cache: str | None = None
        self._vraie_mesure_annoncee = False

    def _get_free_vram_mb(self) -> int | None:
        """VRAM libre en Mo, ou `None` si elle n'a pas pu être MESURÉE.

        On évite torch.cuda parce qu'il ne voit pas llama.cpp : c'est ce
        pousse-boussole qui décide si un 8B tient.

        `None`, et non 0. Un 0 Mo libres est un AROG de crash mesure, et le
        gestionnaire le lisait comme « la carte est pleine » : il déclenchait un
        repli OOM, une attente de libération et un n_ctx réduit pour une machine
        où personne n'avait rien mesuré. Un refus qui invente sa mesure est un
        refus qui décide au hasard. Donc : absent → absent, et l'appelant en
        tient compte au lieu de comparer.

        L'OUTIL passe par `backend.config.paths` (comme tout le reste du produit) :
        le « nvidia-smi » en dur du PATH était un second niveau de résolution,
        secret, qui ne pouvait nommer ni l'absence ni le remède.
        """
        chemin = self._outil_mesure_vram()
        if chemin is not None:
            try:
                out = subprocess.check_output(
                    [
                        str(chemin),
                        "--query-gpu=memory.free",
                        "--format=csv,noheader,nounits",
                    ],
                    text=True,
                    timeout=5,
                    stderr=subprocess.DEVNULL,
                )
                return int(out.strip().split("\n")[0])
            except Exception as exc:
                self._vraie_mesure_manquante(f"nvidia-smi ({chemin}) muet : {exc}")
        try:
            import pynvml

            pynvml.nvmlInit()
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            info = pynvml.nvmlDeviceGetMemoryInfo(handle)
            return int(info.free) // (1024 * 1024)  # pynvml non typé : int() d'abord
        except Exception:
            pass
        return None

    def _outil_mesure_vram(self) -> str | None:
        """L'outil du pilote, résolu UNE fois puis mémorisé (0 = absent)."""
        if self._outil_vram_cache is None:
            try:
                self._outil_vram_cache = str(exiger_nvidia_smi())
            except DependanceSystemeAbsente as exc:
                self._outil_vram_cache = ""
                self._vraie_mesure_manquante(str(exc))
        return self._outil_vram_cache or None

    def _vraie_mesure_manquante(self, raison: str) -> None:
        """Nomme l'absence de mesure, une fois par manager.

        Une fois, parce que la VRAM est sondée en boucle : la répéter à chaque
        appel noierait le journal d'incident, qui est précisément l'endroit où
        il faut la lire.
        """
        if self._vraie_mesure_annoncee:
            return
        self._vraie_mesure_annoncee = True
        logger.warning(
            "[VRAM_NON_MESUREE] %s — la mémoire GPU ne sera pas déduite de cette "
            "mesure manquante : les décisions de chargement se prendront sans elle.",
            raison,
        )

    def _free_vram_octets(self) -> int:
        """VRAM libre en octets, pour un calcul qui EXIGE un nombre.

        Repli sur la mesure du détecteur AU DÉMARRAGE (`self.backend.free_vram`).
        C'est une vraie mesure, mais datée : elle vaut mieux qu'un 0 inventé, et
        le journal le dit pour qu'on ne la prenne pas pour un relevé courant.
        """
        free = self._get_free_vram_mb()
        if free is not None:
            return free * 1024 * 1024
        return int(self.backend.free_vram)

    def _wait_for_vram_release(self, timeout_s: float = 3.0) -> None:
        start = time.monotonic()
        while time.monotonic() - start < timeout_s:
            free = self._get_free_vram_mb()
            if free is None:
                logger.warning(
                    "Attente de libération VRAM abandonnée : VRAM non mesurable sur cette machine."
                )
                return
            if free >= self._min_required_vram_mb:
                return
            time.sleep(0.1)
        logger.warning(
            "VRAM non libérée après déchargement (%s Mo libres, %s requis)",
            self._get_free_vram_mb(),
            self._min_required_vram_mb,
        )

    def _looks_like_oom(self, e: Exception) -> bool:
        """Détecte un échec mémoire même quand llama.cpp dit juste 'Failed to load model'.

        Vu en prod (q-r.md) : après plusieurs allers 8B<->VL, ~4.6 Go libres < 6 Go requis,
        les 4 stratégies échouent avec 'Failed to load model from file' — jamais reconnu
        comme OOM, donc jamais de repli en offload partiel. Sous le seuil VRAM, on le
        traite comme OOM pour déclencher n_gpu_layers réduit.

        Sans mesure de VRAM, ce seuil ne peut pas être consulté : on ne conclut
        alors rien sur cette base (les autres signaux textuels suffisent), parce
        qu'un 0 Mo « libres » transformerait chaque échec de chargement en OOM.
        """
        s = str(e).lower()
        if (
            "out of memory" in s
            or "cuda" in s
            or "cublas" in s
            or "no memory" in s
            or "llama_context" in s
        ):
            return True
        if "failed to load model" in s:
            free = self._get_free_vram_mb()
            return free is not None and free < self._min_required_vram_mb
        return False

    # --- FSM C07 ---
    @property
    def est_pret(self) -> bool:
        """Un modèle est chargé et utilisable (ready ou running).

        Sert à la reprise périodique : elle ne doit pas réveiller un manager
        encore `unloaded`/`loading`. La lecture est verrouillée parce que la FSM
        est écrite depuis d'autres fils.
        """
        with self._state_lock:
            return self._state in ("ready", "running")

    def _set_state(self, new: str, owner: str | None = None):
        with self._state_lock:
            logger.debug(
                f"[FSM] {self._state} -> {new} (owner={owner}, gen={self._generation})"
            )
            self._state = new
            if owner is not None:
                self._owner = owner
            if new == "failed":
                logger.error(f"[FSM] passage en failed (owner={self._owner})")

    def acquire_admission(self, run_id: str, timeout: float = 30.0) -> bool:
        """Réserve la place run lourd unique (C07). Retourne False si timeout."""
        acquired = self._admission.acquire(timeout=timeout)
        if acquired:
            self._admission_owner = run_id
            logger.info(f"[admission] {run_id} acquise")
        else:
            logger.warning(f"[admission] {run_id} timeout")
        return acquired

    def release_admission(self, run_id: str):
        if self._admission_owner == run_id:
            self._admission_owner = None
            try:
                self._admission.release()
            except ValueError:
                pass
            logger.info(f"[admission] {run_id} libérée")

    @contextmanager
    def use_model(
        self,
        name: str,
        estimated_tokens: int = 4096,
        has_image: bool = False,
        *,
        kind: str = "interactive",
    ) -> Generator:
        """Prend le bail `model`, puis charge/sert le modèle demandé.

        L'attente dépend de la nature du travail : le chat interactif attend
        jusqu'à 120 s (il ne doit pas échouer sur une ingestion de fond), tandis
        qu'une ingestion attend 10 s au plus et cède vite au chat — son appelant
        la replanifie (`figures_worker._differe`, `attachment_ingest.depose`).
        """
        from backend.inference.resource_coordinator import (
            ATTENTE_CHAT_S,
            ATTENTE_INGESTION_S,
            get_resource_coordinator,
        )

        attente_s = ATTENTE_INGESTION_S if kind == "ingestion" else ATTENTE_CHAT_S
        coordinator = get_resource_coordinator()
        lease = coordinator.acquire(
            "model", f"model:{name}", timeout_s=attente_s, kind=kind
        )
        try:
            with self._use_model_uncoordinated(
                name, estimated_tokens, has_image
            ) as model:
                yield model
        finally:
            coordinator.release(lease)

    @contextmanager
    def _use_model_uncoordinated(
        self, name: str, estimated_tokens: int = 4096, has_image: bool = False
    ) -> Generator:
        """
        Context manager : `with manager.use_model("LFM2.5-8B") as llm:`
        - Si le bon modele est deja charge → on le reutilise direct (evite 500ms de reload)
        - Sinon → on decharge l'ancien puis on charge le nouveau
        - Garde-fou supports_vision : 8B texte pur ne doit jamais recevoir file:// image (ticket #23852)
        """
        # FSM : réutilisation si même génération
        reuse = self._current_name == name and self._current_model is not None
        if (
            reuse
            and self._current_n_ctx is not None
            and estimated_tokens + REUSE_CONTEXT_MARGIN > self._current_n_ctx
        ):
            # Une fenêtre plus grande n'est utile que si la VRAM le permet
            # vraiment : sinon resolve_n_ctx renvoie la meme valeur et
            # recharger ne ferait que payer le coût sans bénéfice.
            cible = resolve_n_ctx(name, self._free_vram_octets(), estimated_tokens)
            if cible > self._current_n_ctx:
                logger.warning(
                    "Fenêtre %s insuffisante (%s < %s, cible=%s) : rechargement",
                    name,
                    self._current_n_ctx,
                    estimated_tokens + REUSE_CONTEXT_MARGIN,
                    cible,
                )
                reuse = False
        if reuse:
            self._set_state("running", owner=name)
            try:
                yield self._current_model
            finally:
                self._set_state("ready", owner=name)
            return

        if self._current_model is not None:
            logger.info(
                "VRAM avant unload %s: %s Mo libres",
                self._current_name,
                self._get_free_vram_mb(),
            )
            self._set_state("unloading", owner=self._current_name)
            self._unload_current()
            # Vérifie libération confirmée, sinon failed C07
            free = self._get_free_vram_mb()
            # `None` = non mesuré : on ne peut ni confirmer ni infirmer la
            # libération. Le journal le dit, et on n'invente pas un « 0 Mo libres »
            # qui déclencherait un failed sur une machine dont personne n'a
            # mesuré la carte.
            if free is not None and free < self._min_required_vram_mb:
                logger.warning(
                    f"[FSM] libération non confirmée ({free} Mo), passage failed"
                )
                # Ne bloque pas tout : on tente quand même le chargement mais on note
            elif free is None:
                logger.warning("[FSM] libération non confirmable : VRAM non mesurable")
            self._generation += 1  # invalide anciens handles C07
            self._set_state("unloaded")
            logger.info(
                "VRAM après unload: %s Mo libres gen=%s", free, self._generation
            )

        self._set_state("loading", owner=name)
        try:
            model = self._load_model(name, estimated_tokens, has_image)
        except Exception as e:
            self._set_state("failed", owner=name)
            raise
        self._current_model = model
        self._current_name = name
        self._set_state("ready", owner=name)
        self._set_state("running", owner=name)
        try:
            yield model
        finally:
            # Retour ready, garde chaud
            if self._state != "failed":
                self._set_state("ready", owner=name)

    def _build_llm_kwargs(
        self,
        cfg,
        n_ctx: int,
        flash_attn: bool,
        mmproj: bool = True,
        extra_layers: int | None = None,
    ) -> dict:
        """Construit kwargs Llama selon config + flash_attn + mmproj (corrige llama_chat_handler -> llama_chat_format)."""
        # Vérif en amont : module correct existe
        try:
            import llama_cpp.llama_chat_format  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "llama_cpp.llama_chat_format introuvable — verifiez version llama-cpp-python"
            ) from exc

        kwargs: dict = {
            "model_path": cfg.path,
            "n_ctx": n_ctx,
            "n_gpu_layers": extra_layers
            if extra_layers is not None
            else cfg.n_gpu_layers,
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
                raise RuntimeError(
                    f"{cfg.path} requires a multimodal projector (mmproj), but none is configured."
                )
            if mmproj:
                from pathlib import Path as _P

                if not _P(cfg.mmproj).exists():
                    raise RuntimeError(
                        f"mmproj introuvable: {cfg.mmproj} (GGUF {cfg.path})"
                    )
                kwargs["mmproj"] = cfg.mmproj
                logger.info(f"  mmproj charge: {cfg.mmproj}")
            if not cfg.chat_handler:
                raise RuntimeError(
                    f"{cfg.path} is multimodal but no chat_handler configured."
                )
            # Mapping handler config -> classe réelle (0.3.34 n'a pas LFM2VL, fallback documenté)
            handler = self._build_vision_handler(cfg, cfg.mmproj if mmproj else None)
            kwargs["chat_handler"] = handler
            self._current_chat_handler = handler
            logger.info(
                f"  chat_handler: {cfg.chat_handler} -> {handler.__class__.__name__}"
            )
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
        # (Autrefois, la branche ocr servait GLM-OCR ; ce GGUF n'a jamais été livré — conservée
        # par compat, plus aucun modèle configuré ne l'utilise depuis le brouillon).
        if cfg.chat_handler == "ggml-vision-chat":
            candidates = [
                "LFM2VLChatHandler",
                "Qwen25VLChatHandler",
                "Llava15ChatHandler",
                "Llava16ChatHandler",
                "MTMDChatHandler",
            ]
        elif cfg.chat_handler == "ggml-ocr-chat":
            candidates = [
                "Qwen25VLChatHandler",
                "Llava15ChatHandler",
                "MTMDChatHandler",
            ]
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
        raise RuntimeError(
            f"Aucun handler multimodal compatible pour {cfg.path} (tentes {candidates}): {last_err}"
        )

    def _load_model(self, name: str, estimated_tokens: int, has_image: bool = False):
        """Charge un modele depuis le disque avec les bons parametres (mmproj + chat_handler + flash_attn par modele)."""
        # Un chargement raté ne doit pas laisser croire qu'une fenêtre est active.
        self._current_n_ctx = None
        if Llama is None:
            raise RuntimeError(
                "llama_cpp non installe — lance : pip install llama-cpp-python"
            )

        cfg = MODEL_CONFIGS[name]

        # Brouillon : PaddleOCR-VL-1.6 ne passe JAMAIS par llama.cpp (worker transformers
        # persistant : backend/ingestion/paddle_client.py). Fail loud si quelqu'un tente.
        if name == "PADDLEOCR-VL-1.6":
            raise ValueError(
                "PADDLEOCR-VL-1.6 ne se charge pas via le manager llama.cpp "
                "(worker transformers : backend/ingestion/paddle_client.py)"
            )

        # Garde-fou vision : 8B texte pur ne doit jamais recevoir d'image (ticket #23852)
        if not cfg.supports_vision and has_image:
            raise ValueError(
                f"[supports_vision] {name} texte pur ne supporte pas vision (supports_vision=False) — utilisez LFM2.5-VL-3B. Ticket #23852"
            )

        # n_ctx adaptatif sur VRAM COURANTE (pas le cache détecteur du démarrage) :
        # après plusieurs allers 8B<->VL, la VRAM libre réelle peut être très basse et
        # garder 32K de KV fait échouer le chargement ("Failed to load model", q-r.md).
        # `_free_vram_octets` retombe sur la mesure du détecteur si la courante est
        # absente : datée, mais réelle — là où un 0 serait une décision au hasard.
        current_free_bytes = self._free_vram_octets()
        n_ctx = resolve_n_ctx(name, current_free_bytes, estimated_tokens)
        logger.info(
            f"Chargement {name}: n_ctx={n_ctx}, flash_attn={cfg.flash_attn}, mmproj={cfg.mmproj}"
        )

        # Si VRAM basse, attendre une vraie libération avant de tenter : les
        # libérations CUDA sont asynchrones, foncer dedans = échec de chargement
        free_avant = self._get_free_vram_mb()
        if free_avant is not None and free_avant < self._min_required_vram_mb:
            logger.warning(
                "VRAM basse avant chargement %s, attente libération...", name
            )
            self._wait_for_vram_release(timeout_s=8.0)

        errors: list[tuple[str, Exception]] = []

        # Stratégie retry distingue OOM vs config (Réponses.md n°9)
        LOAD_STRATEGIES = [
            ("nominal", {"flash": cfg.flash_attn, "mmproj": True, "delay": 0}),
            ("apres_gc_force", {"flash": cfg.flash_attn, "mmproj": True, "delay": 2.0}),
            ("flash_off", {"flash": False, "mmproj": True, "delay": 0}),
            ("sans_mmproj", {"flash": False, "mmproj": False, "delay": 0}),
        ]
        # Un modèle vision SANS son projector ne peut rien faire : _build_vision_handler
        # exige mmproj_path, donc sans_mmproj échoue TOUJOURS avec "mmproj_path requis" —
        # un message qui ENTERRE la vraie erreur (VRAM ? handler ? fichier ?). On saute
        # cette stratégie pour les modèles vision (constaté sur GLM-OCR ; ce GGUF n'a d'ailleurs jamais été livré, brouillon : PaddleOCR-VL)
        # q-r.md 09/09 — le GGUF n'a d'ailleurs jamais été livré, brouillon : PaddleOCR-VL)
        # afin que l'erreur surfacée soit la cause réelle. Pour le 8B texte, on la garde
        # (il peut tourner sans projector... il n'en a pas).
        strategies = [
            s
            for s in LOAD_STRATEGIES
            if not (cfg.supports_vision and s[0] == "sans_mmproj")
        ]

        for strat_name, opts in strategies:
            if opts["delay"]:
                gc.collect()
                time.sleep(opts["delay"])
                self._wait_for_vram_release(timeout_s=5.0)
            # Si OOM déjà détecté (y compris "Failed to load model" en VRAM basse),
            # tente directement n_gpu_layers réduit sur VRAM courante
            if any(self._looks_like_oom(e) for _, e in errors):
                try:
                    current_free_b = self._free_vram_octets()
                    partial = estimate_layers(current_free_b, 5 * GB, 1 * GB, 24)
                    # Le repli partiel ne concerne que les couches LLM : un modèle vision
                    # GARDE son projector (mmproj=False lève "mmproj_path requis", q-r.md).
                    keep_mmproj = True if cfg.supports_vision else opts["mmproj"]
                    # "Failed to create llama_context" = KV trop gros pour la VRAM restante :
                    # on divise n_ctx par 2 (plancher 4096). Dégradation progressive :
                    # les stratégies suivantes réutilisent ce n_ctx.
                    floor = 4096
                    if any("llama_context" in str(e).lower() for _, e in errors):
                        n_ctx = max(floor, n_ctx // 2)
                        logger.warning(f"{name} repli OOM : n_ctx réduit à {n_ctx}")
                    kwargs = self._build_llm_kwargs(
                        cfg, n_ctx, False, mmproj=keep_mmproj, extra_layers=partial
                    )
                    llm = Llama(**kwargs)
                    logger.info(
                        f"{name} charge en fallback OOM ({strat_name}, partial={partial})"
                    )
                    self._current_n_ctx = n_ctx
                    return llm
                except Exception as e:
                    errors.append((f"{strat_name}_oom_partial", e))
                    logger.error(f"Tentative {strat_name} OOM échouée: {e}")
                    continue
            try:
                kwargs = self._build_llm_kwargs(
                    cfg, n_ctx, opts["flash"], mmproj=opts["mmproj"]
                )
                llm = Llama(**kwargs)
                logger.info(f"{name} charge avec succes ({strat_name})")
                self._current_n_ctx = n_ctx
                return llm
            except Exception as e:
                errors.append((strat_name, e))
                logger.error(f"Tentative {strat_name} echouee pour {name}: {e}")
                # Si erreur n'est pas OOM, continue vers flash_off etc.
                continue

        raise RuntimeError(
            f"Impossible de charger {name} apres {len(errors)} tentatives: {errors}"
        ) from errors[-1][1]

    def _unload_current(self):
        """Decharge le modele actuel et libere la VRAM/RAM — ferme handler multimodal explicitement."""
        self._current_n_ctx = None
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
                _close = getattr(model, "close", None)
                if callable(_close):
                    _close()
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
        time.sleep(
            0.5
        )  # laisse les libérations CUDA asynchrones se propager à nvidia-smi
        self._wait_for_vram_release(timeout_s=5.0)
        # torch vide uniquement si dispo — ne libère pas llama.cpp mais harmless
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass
        # Les arènes glibc du process backend restent résidentes après la
        # fermeture du modèle texte ; les rendre réduit la pression système
        # avant le lancement du worker image. Best-effort : toutes les libc
        # n'exposent pas `malloc_trim`.
        try:
            import ctypes

            ctypes.CDLL("libc.so.6").malloc_trim(0)
        except (OSError, AttributeError):
            pass

    def context_window(self, name: str | None = None) -> int | None:
        """Fenêtre réelle du modèle chargé, ou None s'il ne l'est pas."""
        if name is not None and self._current_name != name:
            return None
        return self._current_n_ctx

    def unload_all(self):
        """Decharge tout (appele a la fermeture de l'app)."""
        self._unload_current()
