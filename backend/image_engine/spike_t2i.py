"""Spike T2I : transformer lazy (RawVRAM + LazyLinear, zero dense) + encodeur natif + VAE reel.

Echec bruyant si un seul parametre reste non lie (pas de poids random silencieux).
Lancement : `python3 -m backend.image_engine.spike_t2i` depuis la racine projet
(chemins absolus via core.paths — plus de cwd ni sys.path).
"""

import os, sys

_PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from backend.image_engine.runtime.parent_guard import arm_parent_death_signal

arm_parent_death_signal()

import gc
import os

os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

import torch
import torch.nn as nn
from pathlib import Path

from backend.image_engine.core.paths import (
    TRANSFORMER_GGUF,
    TEXT_ENCODER_GGUF,
    VAE_DIR,
    TOKENIZER_DIR,
    TEMP_DIR,
)

from backend.image_engine.loaders.diffusers_shim import ensure_diffusers_loaders

ensure_diffusers_loaders()  # contourne diffusers/loaders (transformers->scipy vs numpy2)

from backend.image_engine.core.config import FLUX2_KLEIN_CONFIG
from backend.image_engine.loaders.gguf_loader import Flux2KleinGGUFLoader
from backend.image_engine.loaders.raw_vram import RawVRAMIndex
from backend.image_engine.models.transformer import Flux2KleinTransformer, LazyLinear
from backend.image_engine.models.vae import Flux2KleinVAE
from backend.image_engine.pipeline.scheduler import Flux2KleinScheduler
from backend.image_engine.runner.memprof import PorteMemoireFranchie, RunRecord
from backend.image_engine.runner.mempwatch import SuiviMemoire
from backend.image_engine.runtime.device_probe import probe_device
from backend.image_engine.runtime.placement import (
    enforce_generation_memory,
    plan_generation_memory,
)


from backend.image_engine.models.transformer_lazy import (
    build_lazy_transformer,
)  # source unique (unifie spike + orchestrateur)

from backend.image_engine.masked_loop import sample_masked
from backend.image_engine.masked_sampling import (
    apply_mask,
    blend_known,
    prepare_latent_mask,
)
from backend.image_engine.source_ref import resolve_source_reference_mode


def _model_timestep(sigma: float) -> float:
    return sigma * 1000.0


def _reset_peak(device: torch.device) -> None:
    if device.type == "cuda":
        torch.cuda.reset_peak_memory_stats(device)


def _peak_gib(device: torch.device) -> float:
    """Pic de VRAM RÉSERVÉE (haut du réservoir du cacheur), en Gio.

    Convention du dépôt (`mempwatch.CONVENTION_MESURE`) : la VRAM se mesure par
    `max_memory_reserved`, pas par `max_memory_allocated`. Le réservé est ce que le
    process TIENT réellement sur la carte — blocs du cacheur compris — donc c'est
    lui qui décide si une autre application peut encore cohabiter. Mesurer
    l'alloué ferait baisser le pic dès que le cacheur libère, c'est-à-dire à
    l'instant le plus favorable à la mesure, et non à celui où la carte manque.
    """
    if device.type != "cuda":
        return 0.0
    return float(torch.cuda.max_memory_reserved(device) / 1024**3)


def rendre_la_memoire_anonyme() -> None:
    """Rend à l'OS les pages que glibc garde en réserve après les déquantisations.

    `malloc_trim(0)` est POSIX-1.2 mais n'existe QUE chez glibc. Sur une libc qui
    ne l'a pas, on le DIT au lieu de le faire semblant : la porte RAM mesurera
    alors une mémoire qui comprend la réserve de l'allocateur, et c'est un fait
    qu'il faut pouvoir nommer le jour où une porte tombe.
    """
    gc.collect()
    try:
        import ctypes

        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError) as exc:
        # `AttributeError` : la libc existe mais n'expose pas `malloc_trim` (musl,
        # par exemple). Ce n'est pas une panne de mesure, c'est un fait à nommer.
        print(
            f"[MEM] malloc_trim indisponible ({exc}) : RssAnon comprendra la réserve de l'allocateur",
            flush=True,
        )


def build_cond_refs(
    *, mask_active: bool, source_ref_latent, extra_refs, keep_source_reference: bool
):
    """Retourne la liste ref_latents passée au transformer.

    Règles exactes (source référence séparée) :
    - masque inactif => extra_refs ;
    - source_ref_latent nul => extra_refs ;
    - masque inactif ou keep_source_reference faux => extra_refs ;
    - sinon => [source_ref_latent] + extra_refs si extra_refs sinon source_ref_latent.
    """
    if not bool(mask_active):
        return extra_refs
    if source_ref_latent is None:
        return extra_refs
    if not bool(keep_source_reference):
        return extra_refs
    if extra_refs:
        return [source_ref_latent] + list(extra_refs)
    return source_ref_latent


@torch.inference_mode()
def main():
    import argparse
    import ctypes
    import resource

    # Verrouillage des pages : best effort, jamais bloquant. ctypes ne convertit
    # pas -1 en OSError, donc on lit le code de retour et errno explicitement.
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        rc = libc.mlockall(3)
    except (OSError, AttributeError) as exc:
        rc = -1
        _errno_val = getattr(exc, "errno", None)
        if _errno_val is None:
            _errno_val = ctypes.get_errno()
    else:
        _errno_val = ctypes.get_errno() if rc != 0 else 0

    if rc == 0:
        print("[MEM] mlockall succès (MCL_CURRENT|MCL_FUTURE)", flush=True)
    else:
        if not _errno_val:
            _errno_val = ctypes.get_errno()
        print(
            f"[MEM] mlockall échec errno={_errno_val} (best-effort)",
            flush=True,
        )

    try:
        _soft, _hard = resource.getrlimit(resource.RLIMIT_MEMLOCK)
        print(
            f"[MEM] RLIMIT_MEMLOCK soft={_soft} hard={_hard}",
            flush=True,
        )
    except (OSError, ValueError):
        pass

    try:
        with open("/proc/self/status", "r", encoding="utf-8") as _status_file:
            for _line in _status_file:
                if _line.startswith("VmLck:"):
                    print(f"[MEM] {_line.strip()}", flush=True)
                    break
    except OSError:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt-file", required=True)
    ap.add_argument("--width", type=int, required=True)
    ap.add_argument("--height", type=int, required=True)
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--output", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--official-adaln", action="store_true")
    ap.add_argument("--init-image", default=None)
    ap.add_argument("--mask", default=None)
    ap.add_argument("--mode", default="global")
    ap.add_argument("--strength", type=float, default=0.65)
    ap.add_argument(
        "--ref-image",
        "--reference-image",
        action="append",
        dest="ref_images",
        default=None,
    )
    ap.add_argument(
        "--source-rect",
        dest="source_rect",
        default=None,
        help="X,Y,W,H (float) rect source dans la work, pour mode original.",
    )
    a = ap.parse_args()
    if a.width <= 0 or a.height <= 0 or a.width % 16 or a.height % 16:
        raise ValueError(
            "[BAD_ARGS] width et height doivent être des multiples de 16 strictement positifs."
        )
    if a.seed < 0 or a.seed > (1 << 32) - 1:
        raise ValueError("[BAD_ARGS] seed doit être un entier entre 0 et 4294967295.")
    prompt_path = Path(a.prompt_file).expanduser().resolve()
    try:
        prompt = prompt_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(
            f"[PROMPT_UNREADABLE] Prompt image inaccessible : {exc}"
        ) from exc
    if not prompt.strip():
        raise ValueError("[EMPTY_QUERY] Prompt image vide.")
    dev = probe_device()
    device = torch.device(dev.torch_device)
    if device.type != "cuda":
        raise RuntimeError("[DEVICE_UNSUPPORTED] Le worker T2I FLUX.2 nécessite CUDA.")
    print(f"[Device] {dev.name} ({device}) free {dev.free_vram_gb:.1f} Go", flush=True)
    plan = plan_generation_memory(
        dev,
        a.width,
        a.height,
        mode=os.environ.get("THOT_IMAGE_MEMORY_MODE", "auto"),
    )
    print(
        f"[Memory] mode={plan.mode} working={plan.working_gib:.2f} Gio "
        f"budget={plan.budget_gib:.2f} Gio tiling={plan.use_tiling} "
        f"fits={plan.fits} ({plan.reason})",
        flush=True,
    )
    enforce_generation_memory(plan)

    # ── PORTES APPLIQUÉES, ÉTAPE 1 : LE PRÉFLIGHT (avant tout chargement) ──
    # `gates_ok()` n'avait AUCUN appelant : ce fichier se contentait d'imprimer
    # un RunRecord, et une mesure affichée n'est pas une mesure opposée. Ici la
    # porte REFUSE, et elle refuse avant les 5,7 Go de poids : le coût d'un refus
    # au démarrage est d'une seconde, celui d'un refus après une diffusion
    # minuteuse est l'image entière.
    # Elle est placée APRÈS `enforce_generation_memory` (qui raisonne sur une
    # ESTIMATION) et AVANT `_reset_peak` (le premier contact réel avec CUDA) :
    # ainsi un refus ne dépend d'aucun accès GPU et se produit sur toute machine,
    # y compris sur celle qui n'en a pas.
    preflight = RunRecord.capture(dev, "spike")
    print(f"[Portes] preflight : {preflight}", flush=True)
    preflight.exiger_conforme(etape="démarrage")

    # Le suivi de scénario : le PIC du run, avec l'instant où il a eu lieu. Il
    # est ouvert ici et échantillonné à chaque étape clé, parce qu'un relevé
    # pris à la fin ne verrait que le silence d'après le pic.
    suivi = SuiviMemoire(f"diffusion t2i {a.width}x{a.height}")
    _reset_peak(device)
    suivi.observer("après le préflight")

    def stage(sid: str, label: str):
        # Progression structurée pour Thot (pont flux_spike → run.stage) : STAGE: id :: label.
        print(f"STAGE: {sid} :: {label}", flush=True)

    stage("demarrage", "Préparation")

    # 1. Encode (natif, parite 0.9986) puis release total Qwen.
    # Replique pipeline klein : chat_template + pad 512 (tokens reels + pads conditionnent).
    from tokenizers import Tokenizer
    from jinja2 import Environment, FileSystemLoader

    tok = Tokenizer.from_file(str(TOKENIZER_DIR / "tokenizer.json"))
    tpl = Environment(loader=FileSystemLoader(str(TOKENIZER_DIR))).get_template(
        "chat_template.jinja"
    )
    templated = tpl.render(
        messages=[{"role": "user", "content": prompt}],
        add_generation_prompt=True,
        enable_thinking=False,
        bos_token="<|endoftext|>",
        eos_token="<|im_end|>",
        pad_token="<|endoftext|>",
    )
    encoded_ids = tok.encode(templated).ids[:512]
    real_length = len(encoded_ids)
    ids = encoded_ids + [151643] * (512 - real_length)
    attention_mask = [1] * real_length + [0] * (512 - real_length)
    print(
        f"templated: {len(ids)} tokens (bruts {len(tok.encode(templated).ids)})",
        flush=True,
    )
    stage("texte-chargement", "Chargement de l'encodeur texte (Qwen3-8B)")
    qwen_loader = Flux2KleinGGUFLoader(str(TEXT_ENCODER_GGUF))
    from backend.image_engine.models.qwen3_forward import Qwen3NativeEncoder

    enc = Qwen3NativeEncoder(qwen_loader, device=device)
    stage("texte-inference", "Encodage du prompt")
    taps, _ = enc.forward_taps(
        torch.tensor([ids]), attention_mask=torch.tensor([attention_mask])
    )
    print("taps", tuple(taps.shape), flush=True)
    qwen_peak_gib = _peak_gib(device)
    print(
        f"[MEM] Qwen peak={qwen_peak_gib:.2f} Gio "
        f"current={torch.cuda.memory_allocated(device) / 1024**3:.2f} Gio",
        flush=True,
    )
    del enc
    qwen_loader.seal()
    qwen_loader.clear()
    gc.collect()
    torch.cuda.empty_cache()
    # `malloc_trim` fait partie de la recette de libération du rapport
    # (« del reader; gc.collect(); malloc_trim(0) ») et manquait ici. Il rend au
    # système les pages que glibc garde en réserve après la déquantisation : sans
    # lui, RssAnon continue de monter alors que le process n'occupe plus rien, et
    # la porte RAM juge une mémoire que l'OS peut déjà réutiliser. Ce n'est pas une
    # mesure plus indulgente : c'est la mesure de ce que le process TIENT, et pas
    # de ce que son allocateur a mis de côté.
    rendre_la_memoire_anonyme()
    print(
        f"[MEM] Qwen released current={torch.cuda.memory_allocated(device) / 1024**3:.2f} Gio",
        flush=True,
    )
    suivi.observer("encodeur texte : pic d'encodage")
    _reset_peak(device)
    suivi.observer("encodeur texte libéré")

    stage("texte-liberation", "Libération de l'encodeur texte")

    # 2. Transformer lazy
    stage("transformer-chargement", "Chargement du transformer (VRAM brute)")
    model_path = a.model or str(TRANSFORMER_GGUF)
    tr, index = build_lazy_transformer(
        model_path, FLUX2_KLEIN_CONFIG, device=device, official_adaln=a.official_adaln
    )
    transformer_build_peak_gib = _peak_gib(device)
    suivi.observer("assemblage du transformer")
    _reset_peak(device)
    stage("transformer-assemblage", "Assemblage + liaison des poids")
    stage("vae-chargement", "Chargement du VAE")
    vae = Flux2KleinVAE(
        FLUX2_KLEIN_CONFIG, vae_path=str(VAE_DIR), device=device, dtype=torch.bfloat16
    )
    if getattr(vae, "_vae", None) is None:
        raise RuntimeError("[VAE_UNAVAILABLE] Le VAE FLUX.2 n'a pas été chargé.")
    vae.eval()
    vae.configure_memory(tiling=plan.use_tiling, slicing=plan.use_slicing)
    vae_load_peak_gib = _peak_gib(device)
    suivi.observer("chargement du VAE")
    _reset_peak(device)
    sched = Flux2KleinScheduler(num_steps=4)

    # 3. Sample 4 steps — sigmas klein (mu dynamique sur seq_len image)
    W, H = a.width, a.height
    sched.set_timesteps(4, image_seq_len=(H // 16) * (W // 16), device="cpu")
    print("sigmas:", [round(float(sig), 4) for sig in sched.sigmas], flush=True)
    import numpy as _np
    import torch.nn.functional as _F
    from PIL import Image as _PILImage

    a_mode = (a.mode or "global").strip().lower()
    init_path = getattr(a, "init_image", None)
    mask_lat_bool = None
    start_index = 0
    source_latent = None
    noise: torch.Tensor | None = None
    mask_path = None
    ref_image_paths = [
        str(p) for p in (getattr(a, "ref_images", None) or []) if str(p).strip()
    ]
    reference_latents = []
    if not init_path:
        torch.manual_seed(a.seed)
        torch.cuda.manual_seed(a.seed)
        lat = torch.randn(1, 32, H // 8, W // 8, device=device, dtype=torch.bfloat16)
    else:
        img = _PILImage.open(str(init_path))
        if img.mode == "RGBA":
            print(
                "[WORK_ALPHA] image initiale RGBA : pixels transparents aplatis sur gris 128 — vérifier que le staging envoie bien la source propre.",
                flush=True,
            )
            background = _PILImage.new("RGB", img.size, (128, 128, 128))
            background.paste(img, mask=img.getchannel("A"))
            img = background
        img = img.convert("RGB").resize((W, H), _PILImage.LANCZOS)
        image_tensor = (
            torch.from_numpy(_np.asarray(img))
            .permute(2, 0, 1)
            .unsqueeze(0)
            .to(dtype=torch.float32)
        )
        image_tensor = (image_tensor / 127.5) - 1.0
        image_tensor = image_tensor.to(device=device, dtype=torch.bfloat16)
        x0 = vae.encode(image_tensor)
        mask_path = getattr(a, "mask", None)
        if mask_path:
            m = (
                _PILImage.open(str(mask_path))
                .convert("L")
                .resize((W, H), _PILImage.NEAREST)
            )
            m_tensor = (
                torch.from_numpy(_np.asarray(m))
                .unsqueeze(0)
                .unsqueeze(0)
                .to(dtype=torch.float32, device=device)
            )
            m_tensor = m_tensor.to(dtype=torch.uint8)
            mask_lat_bool = (
                prepare_latent_mask(m_tensor, (H // 8, W // 8), convention="uint8")
                .expand(1, 32, H // 8, W // 8)
                .clone()
            )
        torch.manual_seed(a.seed)
        torch.cuda.manual_seed(a.seed)
        noise = torch.randn_like(x0)
        strength = float(getattr(a, "strength", 0.65))
        num_steps = 4
        init_timestep = min(num_steps * strength, num_steps)
        start_index = int(max(num_steps - init_timestep, 0))
        start_index = max(0, min(3, start_index))
        t_start = float(sched.sigmas[start_index])
        x_t = blend_known(x0.float(), noise.float(), t_start).to(torch.bfloat16)
        if mask_lat_bool is not None:
            lat = apply_mask(
                x_t,
                blend_known(x0.float(), noise.float(), t_start).to(x_t.dtype),
                mask_lat_bool,
            ).to(torch.bfloat16)
        else:
            lat = x_t
        source_latent = x0
    if not init_path:
        source_latent = None
    if ref_image_paths:
        for _rp in ref_image_paths:
            _img = _PILImage.open(str(_rp))
            if _img.mode == "RGBA":
                _bg = _PILImage.new("RGB", _img.size, (128, 128, 128))
                _bg.paste(_img, mask=_img.getchannel("A"))
                _img = _bg
            _img = _img.convert("RGB").resize((W, H), _PILImage.LANCZOS)
            _tensor = (
                torch.from_numpy(_np.asarray(_img))
                .permute(2, 0, 1)
                .unsqueeze(0)
                .to(dtype=torch.float32)
            )
            _tensor = (_tensor / 127.5) - 1.0
            _tensor = _tensor.to(device=device, dtype=torch.bfloat16)
            _rl = vae.encode(_tensor)
            reference_latents.append(_rl.to(torch.bfloat16))
    print(f"refs={len(reference_latents)}", flush=True)
    _reset_peak(device)
    stage("diffusion", f"Diffusion — pas {start_index + 1}/4")
    _extra_ref = reference_latents if reference_latents else None

    # Parse --source-rect X,Y,W,H (float) (avant résolution du mode).
    _source_rect = None
    _raw_sr = getattr(a, "source_rect", None)
    if _raw_sr is not None and str(_raw_sr).strip():
        _sr_parts = [p.strip() for p in str(_raw_sr).split(",")]
        if len(_sr_parts) != 4:
            raise ValueError("[BAD_ARGS] --source-rect doit être X,Y,W,H.")
        try:
            _source_rect = tuple(float(p) for p in _sr_parts)
        except ValueError as exc:
            raise ValueError(
                "[BAD_ARGS] --source-rect composantes non numériques."
            ) from exc

    # Mode de référence source (nouveau contrat V4 + défaut outpaint=original).
    _mode_raw = (os.environ.get("THOT_IMAGE_SOURCE_REFERENCE") or "").strip().lower()
    if _mode_raw not in {"none", "original", "work"}:
        if os.environ.get("THOT_IMAGE_KEEP_SOURCE_REFERENCE") == "1":
            _mode_raw = "work"
        elif os.environ.get("THOT_IMAGE_NO_SOURCE_REFERENCE") == "1":
            _mode_raw = "none"
        else:
            _mode_raw = (
                "original"
                if (
                    a_mode == "outpaint"
                    and _source_rect is not None
                    and mask_lat_bool is not None
                )
                else "none"
            )
    # Soupapes.
    if os.environ.get("THOT_IMAGE_KEEP_SOURCE_REFERENCE") == "1":
        _mode_raw = "work"
    if os.environ.get("THOT_IMAGE_NO_SOURCE_REFERENCE") == "1":
        _mode_raw = "none"
    _source_reference_mode = (
        _mode_raw if _mode_raw in {"none", "original", "work"} else "none"
    )

    _source_ref_latent = None
    _keep_source_reference = False

    if _source_reference_mode == "work":
        _source_ref_latent = source_latent
        _keep_source_reference = True
    elif _source_reference_mode == "original":
        if (
            _source_rect is not None
            and mask_lat_bool is not None
            and init_path is not None
        ):
            import numpy as _np_crop

            _work_img = _PILImage.open(str(init_path))
            if getattr(_work_img, "mode", None) == "RGBA":
                _bg = _PILImage.new("RGB", _work_img.size, (128, 128, 128))
                _bg.paste(_work_img, mask=_work_img.getchannel("A"))
                _work_img = _bg
            _work_img = _work_img.convert("RGB")
            _rx, _ry, _rw, _rh = _source_rect
            _x0 = max(0, min(int(round(_rx)), _work_img.width))
            _y0 = max(0, min(int(round(_ry)), _work_img.height))
            _x1 = max(_x0 + 1, min(int(round(_rx + _rw)), _work_img.width))
            _y1 = max(_y0 + 1, min(int(round(_ry + _rh)), _work_img.height))
            _box = (_x0, _y0, _x1, _y1)
            _crop = _work_img.crop(_box)
            _cw = (_crop.width // 8) * 8
            _ch = (_crop.height // 8) * 8
            if _cw < 64 or _ch < 64:
                raise ValueError("[BAD_ARGS] --source-rect trop petit.")
            if (_cw, _ch) != (_crop.width, _crop.height):
                _crop = _crop.crop((0, 0, _cw, _ch))
            _crop_tensor = (
                torch.from_numpy(_np_crop.asarray(_crop))
                .permute(2, 0, 1)
                .unsqueeze(0)
                .to(dtype=torch.float32)
            )
            _crop_tensor = (_crop_tensor / 127.5) - 1.0
            _crop_tensor = _crop_tensor.to(device=device, dtype=torch.bfloat16)
            _crop_latent = vae.encode(_crop_tensor)
            _source_ref_latent = _crop_latent.to(torch.bfloat16)
            _keep_source_reference = True

    print(
        f"[SOURCE_REF] mode={_source_reference_mode} rect={_source_rect} "
        f"shape={tuple(_source_ref_latent.shape) if _source_ref_latent is not None else None}",
        flush=True,
    )

    _cond_refs_list = build_cond_refs(
        mask_active=mask_lat_bool is not None,
        source_ref_latent=_source_ref_latent,
        extra_refs=_extra_ref,
        keep_source_reference=_keep_source_reference
        if _source_ref_latent is not None
        else True,
    )

    def _denoise(latent, t):
        v = tr(
            latent,
            torch.tensor([_model_timestep(t)], device=device, dtype=torch.bfloat16),
            taps,
            ref_latents=_cond_refs_list,
        )
        assert v is not None
        print(
            f"step t={float(t):.4f} vmean={v.float().mean().item():.4f} "
            f"vstd={v.float().std().item():.4f} latstd={latent.float().std().item():.4f}",
            flush=True,
        )
        return v

    _sigmas = [float(x) for x in sched.sigmas]
    v = None
    lat, _trace = sample_masked(
        lat=lat,
        sigmas=_sigmas,
        start_index=start_index,
        source_latent=source_latent,
        noise=noise,
        mask_lat_bool=mask_lat_bool,
        denoise=_denoise,
        steps=4,
    )
    for _entry in _trace:
        print(f"step {_entry['step']} ok", flush=True)
        suivi.observer(f"diffusion — pas {_entry['step'] + 1}/4")
        if _entry["step"] < 3:
            stage("diffusion", f"Diffusion — pas {_entry['step'] + 2}/4")
    print(f"STEPS_EXECUTED: {4 - start_index}", flush=True)
    sampling_peak_gib = _peak_gib(device)
    del v, taps, tr, sched
    # L'index mmap du transformer n'est plus lu après la diffusion ; le garder
    # jusqu'au bilan fait payer le fichier et retarde la libération des pages.
    # Best-effort : un index déjà relâché n'a plus rien à rendre.
    try:
        index.release()
    except Exception as exc:
        print(f"[MEM] index.release échoué: {exc}", flush=True)
    # Le loader de l'encodeur texte n'a plus d'usage après l'encodage ; retirer
    # la référence résiduelle permet à `malloc_trim` de rendre ses arènes.
    try:
        del qwen_loader
    except NameError:
        pass
    gc.collect()
    if device.type == "cuda":
        torch.cuda.empty_cache()
    # `malloc_trim` rend à l'OS les pages devenues libres (et donc les entrées
    # de swap correspondantes) AVANT que le bilan ne mesure le résidu.
    rendre_la_memoire_anonyme()
    print(
        f"[MEM] Transformer peak={transformer_build_peak_gib:.2f} Gio "
        f"sampling peak={sampling_peak_gib:.2f} Gio "
        f"VAE load peak={vae_load_peak_gib:.2f} Gio",
        flush=True,
    )
    _reset_peak(device)
    stage("vae-inference", "Décodage de l'image (VAE)")
    if getattr(vae, "_vae", None) is None:
        raise RuntimeError("[VAE_UNAVAILABLE] Le VAE FLUX.2 n'a pas été chargé.")
    img = vae.decode(lat, recover_oom=True)
    suivi.observer("décodage VAE")
    img = ((img + 1) / 2).clamp(0, 1).squeeze(0).permute(1, 2, 0).cpu().float().numpy()
    if img.shape[1] != W or img.shape[0] != H:
        raise RuntimeError(
            f"[DIMENSIONS_MISMATCH] VAE a produit {img.shape[1]}x{img.shape[0]}, attendu {W}x{H}."
        )
    from PIL import Image

    stage("sauvegarde", "Sauvegarde")
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    out_path = a.output or str(TEMP_DIR / "spike_t2i.png")
    Image.fromarray((img * 255).astype("uint8")).save(out_path, format="PNG")
    print("saved", out_path, flush=True)

    vae_peak_gib = _peak_gib(device)
    overall_peak_gib = max(
        qwen_peak_gib,
        transformer_build_peak_gib,
        sampling_peak_gib,
        vae_load_peak_gib,
        vae_peak_gib,
    )
    suivi.observer("bilan de run")
    rec = RunRecord.capture(dev, "spike", vram_peak_mb=overall_peak_gib * 1024)
    print("RUNRECORD:", rec, flush=True)
    print(
        f"VRAM pic Go: Qwen={qwen_peak_gib:.2f} transformer={transformer_build_peak_gib:.2f} "
        f"sampling={sampling_peak_gib:.2f} VAE={vae_peak_gib:.2f} "
        f"total={overall_peak_gib:.2f}",
        flush=True,
    )
    # Le PIC du scénario, avec l'instant où il a eu lieu : le relevé ci-dessus est
    # le VRAM des phases lues à la main, celui-ci est le maximum du CUMUL
    # (RssAnon + VRAM) sur l'ensemble des instants observés. Les deux ne se
    # remplacent pas — le premier alimente les portes, le second dit où ça pète.
    print(f"PIC SCENARIO: {suivi.pic()}", flush=True)

    # ── PORTES APPLIQUÉES, ÉTAPE 2 : LE BILAN ──
    # Même refus, au bout de la course : une image produite hors budget n'est pas
    # une image conforme à la Bible, et la publier quand même rendrait la mesure
    # décorative. Le PNG produit à l'instant d'avant est donc EFFACÉ : un artefact
    # hors budget laissé sur le disque finirait par être ramassé par un balayage de
    # `temp/`, et il aurait alors l'air d'une image valide.
    #
    # ÉTAT MESURÉ AU 06/10/2026, sur cette machine (1024², Q4_K_M, 12 Go) : ce bilan
    # PASSE, et il a raison. RssAnon 1 498 Mo, VRAM pic 2 605 Mo, VmSwap 0,
    # RssFile 261 Mo — les cinq portes vertes, cumul de convention 4 102 Mo sous
    # les 8 Go de la Bible §1.4.
    #
    # CE QUI A CHANGÉ DEPUIS LE 05/10, ET POURQUOI LE BILAN REFUSAIT AVANT. Ce
    # fichier disait alors que `RawVRAMIndex` faisait `arr.copy()` : c'était vrai,
    # et c'est ce qui fit tomber la porte « RssAnon ≤ 3 Go » (6,9 Go, cumul 8,9 Go)
    # — les 5,45 Go du fichier de poids étaient RECOPIÉS en mémoire anonyme. Le
    # défaut est réparé : `RawVRAMIndex` sert des vues sur un mmap MAP_PRIVATE, donc
    # le fichier n'entre plus dans `RssAnon`, et il entre dans `RssFile`, qui est la
    # porte qui lui revient (`mempwatch.CONVENTION_MESURE`).
    #
    # LA PORTE `RssFile` A DONC CHANGÉ DE SENS APRÈS LA CORRECTION, et son seuil
    # avec : 1 000 Mo était calibré sur la version qui copiait, donc il passait
    # parce que le fichier était payé ailleurs. Elle se règle désormais sur une
    # mesure de ce code — 261 Mo ici, 5 047 Mo quand le relâchement des pages
    # (`LazyLinear.forward` → `RawVRAMIndex.relacher_les_pages`) est retiré, seuil
    # 700 Mo — et la preuve qu'elle mord encore est dans
    # `tests/backend/test_portes_memoire.py`. Détail des seuils et de leur origine :
    # `runner/memprof.py`, autour de `RSS_FILE_BASE_MB`.
    try:
        rec.exiger_conforme(etape="bilan")
    except PorteMemoireFranchie:
        try:
            Path(out_path).unlink()
            print(f"[Portes] image hors budget effacée : {out_path}", flush=True)
        except OSError as exc:
            print(
                f"[Portes] image hors budget NON effacée ({exc}) : {out_path}",
                flush=True,
            )
        raise
    finally:
        # L'image est sauvegardée : les tenseurs de décodage et le VAE n'ont plus
        # d'usage. Mais le teardown complet est APRÈS le verdict, pas avant : le
        # bilan doit mesurer l'état de FIN DE RUN (VAE encore résident, VRAM libre
        # non gonflée), sinon il mesurerait un résidu et la porte « VRAM ≤
        # free−0.5 » relâcherait sa contrainte. Les ressources sont néanmoins
        # rendues dans TOUS les cas, y compris quand la porte refuse.
        #
        # Le nettoyage est BEST-EFFORT, et volontairement enveloppé : si
        # `rec.exiger_conforme` a levé `PorteMemoireFranchie`, c'est CE refus que
        # l'appelant doit voir. Un `NameError` ou un OOM de teardown qui
        # s'échapperait d'ici remplacerait la porte par une panne de ménage, et
        # le refus — la seule information utile — serait perdu. On journalise
        # donc l'échec au lieu de l'avaler.
        try:
            del img, lat, vae
            gc.collect()
            if device.type == "cuda":
                torch.cuda.empty_cache()
            rendre_la_memoire_anonyme()
        except Exception as exc:
            print(f"[MEM] teardown incomplet: {exc}", flush=True)


if __name__ == "__main__":
    main()
