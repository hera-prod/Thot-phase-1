from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import math
import os
import re
import secrets
import select as _select
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from backend.image_engine.core import paths as _engine_paths

# Chargement PARESSEUX : ne JAMAIS importer diffusers/torch au simple import de
# ce service ni pendant probe(), pour garder la sonde légère (pas de CUDA).
_OFFICIAL_INPAINT_MODULE = None
_OFFICIAL_INPAINT_ATTEMPTED = False


def _official_inpaint_module():
    global _OFFICIAL_INPAINT_MODULE, _OFFICIAL_INPAINT_ATTEMPTED
    if not _OFFICIAL_INPAINT_ATTEMPTED:
        _OFFICIAL_INPAINT_ATTEMPTED = True
        try:
            from backend.image_engine import official_inpaint as _m

            _OFFICIAL_INPAINT_MODULE = _m
        except Exception:  # pragma: no cover
            _OFFICIAL_INPAINT_MODULE = None
    return _OFFICIAL_INPAINT_MODULE


REPO_ROOT = Path(__file__).resolve().parents[2]
ENGINE_DIR = REPO_ROOT / "backend" / "image_engine"
SPIKE_MODULE = "backend.image_engine.spike_t2i"
BACKEND_NAME = "flux2"


def _tail_pour_echec_worker(log_tail: list[str], max_lines: int = 8) -> str:
    """Les dernières lignes + la ligne PIC SCENARIO, pour nommer l'étape du pic.

    Le diagnostic d'échec ne gardait que `log_tail[-5:]` : la ligne
    `PIC SCENARIO:` — la seule qui nomme l'étape du pic — était perdue dès
    qu'elle sortait de cette fenêtre. On la réinjecte une fois, en tête, puis
    on ajoute les `max_lines` dernières lignes sans la dupliquer.
    """
    entries = [str(line) for line in log_tail]
    pic = None
    for line in reversed(entries):
        if line.startswith("PIC SCENARIO:"):
            pic = line
            break
    recent = entries[-max_lines:] if max_lines > 0 else []
    queue = [line for line in recent if not line.startswith("PIC SCENARIO:")]
    if pic is not None:
        queue.insert(0, pic)
    return sanitize_local_paths(" | ".join(queue))[:1600]


def _parse_steps_executed(log_tail):
    """Parse ``STEPS_EXECUTED: N`` depuis un log (liste ou texte complet)."""
    if log_tail is None:
        return None
    if isinstance(log_tail, str):
        entries = log_tail.splitlines()
    else:
        try:
            entries = list(log_tail)
        except TypeError:
            entries = [log_tail]
    for entry in reversed(entries):
        match = re.search(r"STEPS_EXECUTED:\s*(\d+)", str(entry))
        if match:
            return int(match.group(1))
    if isinstance(log_tail, str):
        match = re.search(r"STEPS_EXECUTED:\s*(\d+)", log_tail)
        if match:
            return int(match.group(1))
    return None


CHECKPOINT = "flux2-klein-9b-kv-Q4_K_M"
MODEL_NAME = "FLUX.2 Klein 9B-KV"
# SEUIL D'ADMISSION, DÉRIVÉ DU PIC MESURÉ — et de rien d'autre.
#
# 7,9 Go ne venait d'aucune mesure de ce code : il venait de `models/README.md`, et
# de rien dans le dépôt ne l'atteste pour cette version. Le 24/09, `docs/09_phases_2_5.md`
# mentionnait un « pic 7,91 Go mesuré » sur un pipeline qui n'est plus celui-ci ; je
# n'ai pas établi pourquoi le chiffre avait changé, et un chiffre qu'on ne sait pas
# expliquer n'est pas une mesure — c'est un souvenir. Le seuil suit donc la mesure
# faite ici, sur CE code.
#
# MESURE DU 05/10/2026, RTX 4070 SUPER, `flux2-klein-9b-kv-Q4_K_M`, 4 pas de
# diffusion, 1024x1024 (la résolution de travail), DEUX REJETS IDENTIQUES :
#
#     VRAM pic = torch.cuda.max_memory_reserved() = 3 134 Mo = 3,06 Go
#     (dont le pic d'ALLOCATION atteint 2,54 Go au décodage VAE)
#     RssAnon 1 497 Mo | RssFile 4 979 Mo | cumul de convention 4 102 Mo = 4,01 Go
#
# C'est le RÉSERVOIR qui est retenu, pas l'allocation, et c'est le même chiffre que
# la porte « VRAM ≤ free − 0,5 » consomme (`mempwatch.CONVENTION_MESURE`) : c'est ce
# que le process TIENT sur la carte, donc ce qui doit y entrer. Prendre l'allocation
# (2,54 Go) aurait produit un seuil trop permissif, parce que le cacheur ne rend pas ce
# qu'il a réservé.
#
# ON Y AJOUTE LA MARGE de 0,5 Go que la porte applique DÉJÀ au moment du verdict
# (`memprof.MARGE_VRAM_LIBRE_GB`). Les deux chiffres disent donc la même chose au même
# endroit : l'admission et la porte ne peuvent plus se contredire, ce qui était le cas
# tant que 9,0 > 7,9 + 0,5.
#
# CE QUE ÇA CHANGE, et qu'il faut dire : une carte de 8 Go (~7,4 Go libres) est
# désormais ADMISSÉE, parce que le pipeline mesuré tient dans 3,06 Go. C'est le prix
# d'un seuil enfin adossé à une mesure — et la contre-épreuve est la porte du bilan,
# qui refuse sur place si la carte ne tient pas en vrai.
#
# LE JOUR OÙ LE PIC CHANGE, CE NOMBRE CHANGE AVEC. L'option Q3 (poids jamais
# ramenés en VRAM) ferait baisser le réservoir, pas l'inverse : le seuil se
# re-dérève sur une mesure, jamais sur un calcul.
PIC_VRAM_MESURE_GB = 3.06
MARGE_ADMISSION_GB = 0.5
MIN_FREE_VRAM_GB = PIC_VRAM_MESURE_GB + MARGE_ADMISSION_GB
READY_NOTE = "Backend FLUX.2 Klein 9B T2I ~1 MP disponible (opt-in actif)"
RUN_TIMEOUT_S = 600
MAX_WORKER_STDOUT_BYTES = 1024 * 1024
MAX_WORKER_LINE_BYTES = 64 * 1024
MAX_PNG_BYTES = 64 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000

_lock = threading.Lock()
_cleanup_blocked: str | None = None


def _parse_progress(text: str) -> dict | None:
    if text.startswith("STAGE:"):
        body = text[len("STAGE:") :].strip()
        sid, _, label = body.partition("::")
        sid, label = sid.strip(), label.strip()
        if not sid:
            return None
        out: dict = {"id": sid, "label": label or sid}
        if sid == "diffusion":
            import re as _re

            match = _re.search(r"pas\s+(\d+)\s*/\s*(\d+)", label)
            if match:
                out["step"], out["total"] = int(match.group(1)), int(match.group(2))
        return out
    return None


def backend_enabled() -> bool:
    value = os.environ.get("THOT_IMAGE_BACKEND", "").strip()
    # "flux2-spike" reste accepté comme alias historique (compat utilisateurs existants).
    if value == "flux2" or value == "flux2-spike":
        return True
    try:
        optin = _optin_path().read_text(encoding="utf-8").strip()
        # "flux2-spike" reste accepté comme alias historique (compat utilisateurs existants).
        if optin == "flux2" or optin == "flux2-spike":
            return True
        return False
    except Exception:
        return False


def official_inpaint_requested() -> bool:
    value = os.environ.get("THOT_USE_OFFICIAL_INPAINT", "0").strip().lower()
    return value not in {"", "0", "false", "no", "off"}


OPTIN_FILENAME = "image_backend.optin"
OPTIN_VALUE = "flux2"


def _optin_path() -> Path:
    override = os.environ.get("THOT_IMAGE_OPTIN_PATH", "").strip()
    if override:
        return Path(override)
    try:
        from backend.db.database import DB_PATH as _DB

        return Path(str(_DB)).parent / OPTIN_FILENAME
    except Exception:
        return REPO_ROOT / "data" / OPTIN_FILENAME


def set_backend_enabled(on: bool) -> Path:
    path = _optin_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if on:
        path.write_text(OPTIN_VALUE + "\n", encoding="utf-8")
    else:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    return path


def _query_gpu_memory() -> tuple[float | None, float | None]:
    """(VRAM libre, VRAM totale) en Go, via l'outil du constructeur de la carte.

    Source UNIQUE de la mesure mémoire : `probe`, `check_install` et `check_vram`
    lisent tous les deux ici, donc un seul `nvidia-smi` à maintenir.

    L'OUTIL EST RÉSOLU PAR `backend.config.paths`, comme partout ailleurs dans le
    produit. Appeler « nvidia-smi » directement signifiait deux choses fausses à la
    fois : le PATH devenait un niveau de résolution secret, et l'échec ne pouvait
    pas être nommé (pas d'explication au refusal, pas de Variable pour un
    installateur non standard). Ici, l'absence remonte par
    `exiger_nvidia_smi()`, qui écrit la conséquence ET le remède ; on n'en
    garde que le verdict `(None, None)`, qui était déjà honnête et le reste.
    """
    from backend.image_engine.runner.mempwatch import lire_vram_carte_gb

    return lire_vram_carte_gb()[:2]


def probe() -> tuple[bool, dict]:
    details: dict = {}
    if getattr(sys, "frozen", False):
        try:
            spike_module_available = (
                SPIKE_MODULE in sys.modules
                or importlib.util.find_spec(SPIKE_MODULE) is not None
            )
        except (ImportError, ValueError):
            spike_module_available = SPIKE_MODULE in sys.modules
    else:
        spike_module_available = (ENGINE_DIR / "spike_t2i.py").exists()
    checks = {
        "spike_module": spike_module_available,
        "transformer_gguf": _engine_paths.TRANSFORMER_GGUF.exists(),
        "text_encoder_gguf": _engine_paths.TEXT_ENCODER_GGUF.exists(),
        "vae": _engine_paths.VAE_WEIGHTS.exists(),
        "tokenizer": (_engine_paths.TOKENIZER_DIR / "tokenizer.json").exists(),
    }
    details["files"] = checks
    free_vram, total_vram = _query_gpu_memory()
    if free_vram is None or total_vram is None:
        details["smi_error"] = "nvidia-smi absent ou illisible"
        details["cuda"] = False
    else:
        details["free_vram_gb"] = free_vram
        details["total_vram_gb"] = total_vram
        details["cuda"] = True
    return all(checks.values()) and bool(details.get("cuda")), details


def check_install() -> tuple[bool, str]:
    """Le pipeline est-il là ? Opt-in, poids, CUDA — SANS regarder la mémoire.

    D'OÙ CE DÉCOUPAGE : libérer le modèle texte est ce qui CRÉE la marge, donc
    mesurer la marge avant la libération mesurait l'occupant et non le besoin.
    Les vérifications ci-dessous sont, elles, indépendantes de la mémoire : un
    install mal configuré doit être refusé SANS déclencher un déchargement
    coûteux du 8B.
    """
    if not backend_enabled():
        return False, (
            "[SERVICE_UNAVAILABLE] Backend image non activé. Activez THOT_IMAGE_BACKEND=flux2 "
            "ou l'interrupteur image avant de lancer une génération T2I."
        )
    ok, details = probe()
    if not ok:
        missing = [key for key, value in details.get("files", {}).items() if not value]
        return (
            False,
            f"[SERVICE_UNAVAILABLE] Pipeline incomplet (manquant : {missing}) / CUDA={details.get('cuda')}.",
        )
    return True, "Backend FLUX.2 Klein 9B T2I ~1 MP installé (opt-in actif)."


def check_vram() -> tuple[bool, str]:
    """La marge VRAM suffit-elle ? À mesurer APRÈS avoir libéré le modèle texte.

    Releve au dernier moment, sur la mémoire réellement disponible : c'est la
    seule mesure qui décrit la capacité de la diffusion à venir. La source reste
    `probe` — un seul endroit résout `nvidia-smi` pour un diagnostic complet
    (`mempwatch.lire_vram_carte_gb`).
    """
    _, details = probe()
    free_vram = details.get("free_vram_gb")
    if not isinstance(free_vram, (int, float)) or free_vram < MIN_FREE_VRAM_GB:
        return False, (
            f"[OUT_OF_MEMORY] VRAM libre insuffisante pour les presets ~1 MP : "
            f"{free_vram if free_vram is not None else 'inconnue'} / {MIN_FREE_VRAM_GB:.1f} Go "
            f"(pic mesuré {PIC_VRAM_MESURE_GB:.1f} Go + {MARGE_ADMISSION_GB:.1f} Go de marge)."
        )
    return True, (
        f"VRAM libre {free_vram:.2f} / {MIN_FREE_VRAM_GB:.1f} Go requis "
        f"(pic mesuré {PIC_VRAM_MESURE_GB:.1f} Go + {MARGE_ADMISSION_GB:.1f} Go de marge)."
    )


def is_available() -> tuple[bool, str]:
    """Les DEUX vérifications, dans l'ordre où elles doivent avoir lieu.

    Pour un diagnostic ponctuel seulement : le chemin de génération appelle
    `check_install` puis `check_vram` DEPUIS la libération du modèle texte ET celle
    du worker OCR (`image_service._rendre_la_vram_du_worker_ocr`). Sonder avant
    ces deux libérations mesurerait l'occupant, pas le besoin.
    """
    ok, reason = check_install()
    if not ok:
        return False, reason
    ok, reason = check_vram()
    if not ok:
        return False, reason
    return True, f"{READY_NOTE} ({reason})"


def _validate_dimensions(width: int, height: int) -> tuple[int, int]:
    if (
        isinstance(width, bool)
        or isinstance(height, bool)
        or not isinstance(width, int)
        or not isinstance(height, int)
        or width <= 0
        or height <= 0
        or width % 16
        or height % 16
    ):
        raise ValueError(
            "[BAD_ARGS] width et height doivent être des multiples de 16 strictement positifs."
        )
    if width * height > MAX_IMAGE_PIXELS:
        raise ValueError(
            f"[TOO_LARGE] Dimensions image trop grandes : {width}x{height}."
        )
    return width, height


def _validate_seed(seed: int | None) -> int:
    if seed is None:
        return secrets.randbits(32)
    if (
        isinstance(seed, bool)
        or not isinstance(seed, int)
        or not 0 <= seed <= (1 << 32) - 1
    ):
        raise ValueError("[BAD_ARGS] seed doit être un entier entre 0 et 4294967295.")
    return seed


def _temp_root() -> Path:
    from backend.config.paths import get_data_dir

    root = Path(get_data_dir()).expanduser().resolve() / "temp"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _write_prompt_file(directory: Path, prompt: str) -> Path:
    path = directory / "prompt.txt"
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            stream.write(prompt)
    finally:
        if descriptor != -1:
            os.close(descriptor)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def _spike_cmd(
    prompt_file: str,
    seed: int,
    width: int,
    height: int,
    out: str,
    *,
    init_image: str | None = None,
    mask: str | None = None,
    mode: str | None = None,
    strength: float | None = None,
    reference_images=None,
    source_rect=None,
) -> tuple[list[str], str | None]:
    arguments = [
        "--prompt-file",
        str(prompt_file),
        "--width",
        str(width),
        "--height",
        str(height),
        "--seed",
        str(seed),
        "--output",
        str(out),
    ]
    if init_image is not None:
        arguments += ["--init-image", str(init_image)]
    if mask is not None:
        arguments += ["--mask", str(mask)]
    if mode is not None:
        arguments += ["--mode", str(mode)]
    if strength is not None:
        arguments += ["--strength", str(strength)]
    if reference_images:
        for _rip in reference_images:
            arguments += ["--ref-image", str(_rip)]
    if source_rect is not None:
        if isinstance(source_rect, (tuple, list)):
            _sr = tuple(source_rect)
        else:
            _sr = tuple(source_rect)
        if len(_sr) != 4:
            raise ValueError("[BAD_ARGS] source_rect doit contenir 4 valeurs.")
        arguments += [
            "--source-rect",
            ",".join(f"{float(v):.2f}" for v in _sr),
        ]
    cwd = str(REPO_ROOT) if REPO_ROOT.exists() else None
    if getattr(sys, "frozen", False):
        return [sys.executable, "--image-worker", *arguments], cwd
    return [sys.executable, "-u", "-m", SPIKE_MODULE, *arguments], cwd


def _signal_process_group(process: subprocess.Popen, requested_signal: int) -> None:
    process_id = getattr(process, "pid", None)
    if os.name == "posix" and process_id is not None:
        try:
            os.killpg(process_id, requested_signal)
            return
        except ProcessLookupError:
            return
        except OSError:
            pass
    if process.poll() is None:
        if requested_signal == signal.SIGTERM:
            process.terminate()
        else:
            process.kill()


def _process_group_exists(process: subprocess.Popen) -> bool:
    process_id = getattr(process, "pid", None)
    if os.name != "posix" or process_id is None:
        return process.poll() is None
    try:
        os.killpg(process_id, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _terminate_process_group(process: subprocess.Popen | None) -> None:
    if process is None:
        return
    _signal_process_group(process, signal.SIGTERM)
    try:
        process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        _signal_process_group(process, signal.SIGKILL)
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"[CLEANUP_FAILED] Worker image non arrêté : {exc}"
            ) from exc
    if process.poll() is None:
        raise RuntimeError("[CLEANUP_FAILED] Worker image non arrêté.")
    if _process_group_exists(process):
        _signal_process_group(process, signal.SIGKILL)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and _process_group_exists(process):
            time.sleep(0.02)
        if _process_group_exists(process):
            raise RuntimeError(
                "[CLEANUP_FAILED] Groupe de processus image encore actif."
            )


def _local_path_roots() -> list[str]:
    candidates: list[Path] = [Path(REPO_ROOT), Path(_engine_paths.DATA_DIR)]
    try:
        from backend.config.paths import get_data_dir

        candidates.append(Path(get_data_dir()))
    except Exception:
        pass
    try:
        candidates.append(Path.home())
    except Exception:
        pass
    roots = {
        str(candidate.expanduser().resolve()).rstrip(os.sep) for candidate in candidates
    }
    return sorted(
        (root for root in roots if root and root != os.sep), key=len, reverse=True
    )


def sanitize_local_paths(value: object) -> str:
    text = str(value)
    for root in _local_path_roots():
        text = text.replace(root, "[chemin local]")
    text = re.sub(
        r"(?i)(?<![A-Za-z0-9:/])[A-Za-z]:[\\/][^\s\"'<>|]+", "[chemin local]", text
    )
    text = re.sub(r"(?i)(?<![A-Za-z0-9:/\]])/(?!/)[^\s\"'<>|]+", "[chemin local]", text)
    text = re.sub(r"(?i)file://[^\s\"'<>|]+", "[chemin local]", text)
    return text


def _append_log(log_tail: list[str], text: str) -> None:
    value = text.strip()
    if not value or re.search(r"(?i)\bsaved\b", value):
        return
    log_tail.append(sanitize_local_paths(value)[:2000])
    del log_tail[:-30]


def _consume_worker_output(
    process: subprocess.Popen,
    cancel_flag,
    deadline: float,
    on_stage,
    log_tail: list[str],
) -> None:
    stream = process.stdout
    if stream is None:
        raise RuntimeError("[INTERNAL_ERROR] Worker image sans stdout.")
    file_descriptor = stream.fileno()
    try:
        os.set_blocking(file_descriptor, False)
    except OSError:
        pass
    pending = bytearray()
    total_bytes = 0

    def process_line(raw: bytes) -> None:
        if len(raw) > MAX_WORKER_LINE_BYTES:
            raise RuntimeError("[INTERNAL_ERROR] Ligne worker image trop volumineuse.")
        text = raw.decode("utf-8", errors="replace").strip()
        _append_log(log_tail, text)
        info = _parse_progress(text)
        if info is not None and on_stage is not None:
            try:
                on_stage(info)
            except Exception:
                pass

    while True:
        if cancel_flag is not None and cancel_flag.is_set():
            raise RuntimeError("[CANCELLED] Run image annulé pendant la diffusion.")
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "[TIMEOUT] Génération image dépassant le délai autorisé."
            )
        try:
            ready, _, _ = _select.select([file_descriptor], [], [], 0.1)
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"[INTERNAL_ERROR] Lecture stdout worker impossible : {exc}"
            ) from exc
        if not ready:
            if process.poll() is not None:
                break
            continue
        try:
            chunk = os.read(file_descriptor, 64 * 1024)
        except BlockingIOError:
            continue
        except OSError as exc:
            raise RuntimeError(
                f"[INTERNAL_ERROR] Lecture stdout worker impossible : {exc}"
            ) from exc
        if not chunk:
            break
        total_bytes += len(chunk)
        if total_bytes > MAX_WORKER_STDOUT_BYTES:
            raise RuntimeError(
                "[INTERNAL_ERROR] stdout worker image dépasse la limite autorisée."
            )
        pending.extend(chunk)
        while b"\n" in pending:
            raw, _, remainder = pending.partition(b"\n")
            pending = bytearray(remainder)
            process_line(bytes(raw).rstrip(b"\r"))
        if len(pending) > MAX_WORKER_LINE_BYTES:
            raise RuntimeError("[INTERNAL_ERROR] Ligne worker image trop volumineuse.")
    if pending:
        process_line(bytes(pending).rstrip(b"\r"))


def _validate_png(png_bytes: bytes, width: int, height: int) -> None:
    if len(png_bytes) > MAX_PNG_BYTES:
        raise RuntimeError(
            "[PNG_TOO_LARGE] Sortie image dépassant la limite autorisée."
        )
    try:
        from PIL import Image
    except Exception as exc:
        raise RuntimeError(
            f"[PNG_INVALID] Pillow indisponible pour valider la sortie : {exc}"
        ) from exc
    try:
        with Image.open(io.BytesIO(png_bytes)) as image:
            image_format = image.format
            image_size = image.size
            if image_format != "PNG":
                raise RuntimeError(
                    f"[PNG_INVALID] Format de sortie {image_format!r}, PNG attendu."
                )
            if image_size[0] * image_size[1] > MAX_IMAGE_PIXELS:
                raise RuntimeError("[PNG_TOO_LARGE] Image décompressée trop grande.")
            image.verify()
        with Image.open(io.BytesIO(png_bytes)) as image:
            image.load()
            if image.size != (width, height):
                raise RuntimeError(
                    f"[DIMENSIONS_MISMATCH] PNG {image.size[0]}x{image.size[1]}, "
                    f"attendu {width}x{height}."
                )
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"[PNG_INVALID] Sortie worker illisible : {exc}") from exc


def _run_t2i_locked(
    prompt: str,
    seed: int,
    width: int,
    height: int,
    timeout_s: float,
    cancel_flag,
    on_stage,
    staged_path: str | Path | None = None,
    *,
    init_image=None,
    mask=None,
    mode=None,
    strength=None,
    reference_images=None,
    source_rect=None,
) -> tuple[bytes, dict]:
    global _cleanup_blocked
    if _cleanup_blocked is not None:
        raise RuntimeError(f"[CLEANUP_FAILED] {_cleanup_blocked}")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("[EMPTY_QUERY] Prompt image vide.")
    width, height = _validate_dimensions(width, height)
    seed = _validate_seed(seed)
    try:
        timeout_s = float(timeout_s)
    except (TypeError, ValueError) as exc:
        raise ValueError("[BAD_ARGS] timeout image invalide.") from exc
    if not math.isfinite(timeout_s) or timeout_s <= 0:
        raise ValueError(
            "[BAD_ARGS] timeout image doit être strictement positif et fini."
        )
    if cancel_flag is not None and cancel_flag.is_set():
        raise RuntimeError("[CANCELLED] Run image annulé avant le démarrage.")
    # --- Parcours officiel (Diffusers) si activé/configuré ---
    normalized_mode = (mode or "").strip().lower() if isinstance(mode, str) else ""
    _oi_mod_for_guard = _official_inpaint_module()
    _allow_download = os.environ.get(
        "THOT_FLUX2_ALLOW_DOWNLOAD", ""
    ).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    _local_files_only = os.environ.get(
        "THOT_FLUX2_LOCAL_FILES_ONLY", "0"
    ).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    _weights_ok = False
    _weights_reason = "model_id_not_local"
    if _oi_mod_for_guard is not None:
        try:
            _weights_ok, _weights_reason = _oi_mod_for_guard.local_weights_compatible()
        except Exception:
            _weights_ok, _weights_reason = False, "model_id_not_local"
    _can_attempt_official = (
        _oi_mod_for_guard is not None
        and _oi_mod_for_guard.is_available()
        and (_weights_ok or (_allow_download and not _local_files_only))
    )
    if (
        mask is not None
        and reference_images
        and normalized_mode in {"local", "inpaint", "outpaint"}
        and official_inpaint_requested()
    ):
        raise RuntimeError(
            "[OFFICIAL_INPAINT_REFERENCES_UNSUPPORTED] La voie officielle d'inpainting "
            "ne prend pas en charge les images de référence. Posez "
            "THOT_USE_OFFICIAL_INPAINT=0 pour utiliser le chemin custom avec références."
        )
    if (
        mask is not None
        and normalized_mode in {"local", "inpaint", "outpaint"}
        and _can_attempt_official
        and official_inpaint_requested()
    ):
        try:
            if on_stage is not None:
                on_stage(
                    "admission", "Préparation de l'inférence officielle (Diffusers)"
                )
            num_inference_steps = 4
            _oi = _official_inpaint_module()
            png_bytes, stats = _oi.run_official_inpaint(
                prompt,
                seed,
                width=width,
                height=height,
                init_image_path=str(init_image),
                mask_image_path=str(mask),
                strength=float(strength if strength is not None else 0.8),
                num_inference_steps=num_inference_steps,
                on_stage=on_stage,
                staged_path=staged_path,
            )
            stats = dict(stats or {})
            try:
                _official_name = _oi.backend_info()
            except Exception:  # pragma: no cover
                _official_name = {}
            _official_name = _official_name or {}
            stats.update(
                {
                    "backend_requested": "official",
                    "backend_effective": stats.get(
                        "backend", "diffusers-flux2-klein-inpaint"
                    ),
                    "pipeline_class": _official_name.get("pipeline_class"),
                    "model_id": _official_name.get("model_id"),
                    "fallback_used": False,
                    "fallback_reason": None,
                    "steps_requested": num_inference_steps,
                    "steps_executed": num_inference_steps,
                }
            )
            return png_bytes, stats
        except Exception as exc:  # pragma: no cover - official path mandatory
            try:
                _oi_clean = _official_inpaint_module()
                if _oi_clean is not None:
                    _oi_clean._PIPELINE = None
                import gc

                gc.collect()
                import torch

                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                pass
            import traceback as _tb

            _trace = "".join(
                sanitize_local_paths(line)
                for line in _tb.format_exception(type(exc), exc, exc.__traceback__)
            )
            sanitized = sanitize_local_paths(str(exc))
            raise RuntimeError(
                f"[OFFICIAL_INPAINT_FAILED] {sanitized}\n{_trace}"
            ) from exc
    # --- Refus explicite : la voie officielle a été DEMANDÉE mais n'est pas là ---
    #
    # Sans cette garde, un `THOT_USE_OFFICIAL_INPAINT=1` sans diffusers tombait
    # silencieusement sur le subprocess custom, puis déclarait
    # `backend_requested: "official"` avec `fallback_used: False` : une trace
    # fausse. Ici on s'arrête AVANT le custom, et on nomme l'alternative.
    official_requested = official_inpaint_requested()
    if (
        official_requested
        and mask is not None
        and normalized_mode in {"local", "inpaint", "outpaint"}
    ):
        _mod = _official_inpaint_module()
        if _mod is None or not _mod.is_available():
            raise RuntimeError(
                "[OFFICIAL_INPAINT_CLASS_UNAVAILABLE] La voie officielle d'inpainting a "
                "été demandée (THOT_USE_OFFICIAL_INPAINT=1), mais "
                "diffusers/Flux2KleinInpaintPipeline est indisponible : aucun fallback "
                "custom n'a été exécuté. Installez diffusers avec "
                "Flux2KleinInpaintPipeline, ou posez THOT_USE_OFFICIAL_INPAINT=0 pour "
                "opter explicitement pour le chemin custom."
            )
        try:
            _w_ok, _w_reason = _mod.local_weights_compatible()
        except Exception:
            _w_ok, _w_reason = False, "model_id_not_local"
        if not _w_ok:
            if _allow_download and _local_files_only:
                raise RuntimeError(
                    "[OFFICIAL_INPAINT_REMOTE_REQUIRED] La voie officielle d'inpainting a "
                    "été demandée avec THOT_FLUX2_ALLOW_DOWNLOAD=1, mais "
                    "THOT_FLUX2_LOCAL_FILES_ONLY est actif : accès distant requis."
                )
            if not (_allow_download and not _local_files_only):
                raise RuntimeError(
                    f"[OFFICIAL_INPAINT_WEIGHTS_INCOMPATIBLE] raison={_w_reason} La voie "
                    "officielle d'inpainting a été demandée (THOT_USE_OFFICIAL_INPAINT=1), "
                    "mais les poids locaux sont incompatibles : aucun fallback custom "
                    "n'a été exécuté."
                )
    started = time.monotonic()
    deadline = started + timeout_s
    process: subprocess.Popen | None = None
    log_tail: list[str] = []
    failure: BaseException | None = None
    with tempfile.TemporaryDirectory(
        prefix="flux_", dir=str(_temp_root())
    ) as temporary:
        directory = Path(temporary)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
        prompt_file = _write_prompt_file(directory, prompt)
        if staged_path is None:
            output_file = directory / "output.png"
        else:
            output_file = Path(staged_path)
            output_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            command, cwd = _spike_cmd(
                str(prompt_file),
                seed,
                width,
                height,
                str(output_file),
                init_image=init_image,
                mask=mask,
                mode=mode,
                strength=strength,
                reference_images=reference_images,
                source_rect=source_rect,
            )
            process = subprocess.Popen(
                command,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
            _consume_worker_output(process, cancel_flag, deadline, on_stage, log_tail)
            try:
                process.wait(timeout=max(1.0, min(5.0, timeout_s)))
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(
                    "[INTERNAL_ERROR] Worker image terminé sans confirmer sa sortie."
                ) from exc
        except BaseException as exc:
            failure = exc
        finally:
            cleanup_error: BaseException | None = None
            try:
                _terminate_process_group(process)
            except BaseException as exc:
                cleanup_error = exc
            if process is not None and process.stdout is not None:
                try:
                    process.stdout.close()
                except Exception:
                    pass
            if cleanup_error is not None:
                _cleanup_blocked = sanitize_local_paths(cleanup_error)[:500]
                if failure is None:
                    failure = RuntimeError(f"[CLEANUP_FAILED] {_cleanup_blocked}")
                else:
                    failure = RuntimeError(
                        f"[CLEANUP_FAILED] Worker image actif après erreur : {failure}. "
                        f"Nettoyage : {_cleanup_blocked}"
                    )
                    failure.__cause__ = cleanup_error
        if failure is not None:
            if isinstance(failure, RuntimeError):
                raise failure
            raise RuntimeError(
                f"[INTERNAL_ERROR] Worker image : {failure}"
            ) from failure
        if process is None or process.returncode != 0:
            code = process.returncode if process is not None else "inconnu"
            tail = _tail_pour_echec_worker(log_tail)
            if "[DEQUANT_FAILED]" in tail:
                raise RuntimeError(
                    f"[DEQUANT_FAILED] Worker image exit={code} : {tail}"
                )
            raise RuntimeError(f"[INTERNAL_ERROR] Worker image exit={code} : {tail}")
        try:
            if output_file.stat().st_size > MAX_PNG_BYTES:
                raise RuntimeError(
                    "[PNG_TOO_LARGE] Sortie image dépassant la limite autorisée."
                )
            png_bytes = output_file.read_bytes()
        except OSError as exc:
            raise RuntimeError(
                f"[INTERNAL_ERROR] Sortie worker illisible : {exc}"
            ) from exc
    _validate_png(png_bytes, width, height)
    duration = round(time.monotonic() - started, 3)
    stats = {
        "backend": BACKEND_NAME,
        "width": width,
        "height": height,
        "dimensions": {"width": width, "height": height},
        "seed": seed,
        "mode": mode
        if mode is not None
        else ("create" if init_image is None else "global"),
        "duration_s": duration,
        "duration": duration,
        "duration_ms": int(duration * 1000),
        "log_tail": log_tail[-8:],
        **({"staged_path": str(output_file)} if staged_path is not None else {}),
    }
    _steps_executed = _parse_steps_executed(log_tail)
    stats = dict(stats)
    stats.update(
        {
            "backend_requested": "custom",
            "backend_effective": BACKEND_NAME,
            "pipeline_class": "backend.image_engine.spike_t2i",
            "model_id": CHECKPOINT,
            "fallback_used": False,
            "fallback_reason": None,
            "steps_requested": 4,
        }
    )
    if stats.get("backend_effective") == BACKEND_NAME and _steps_executed is None:
        raise RuntimeError(
            "[STEPS_UNREPORTED] steps_executed non rapporté par le backend custom."
        )
    stats["steps_executed"] = _steps_executed
    return png_bytes, stats


def run_t2i(
    prompt: str,
    seed: int | None = None,
    timeout_s: int = RUN_TIMEOUT_S,
    cancel_flag=None,
    on_stage=None,
    *,
    width: int = 512,
    height: int = 512,
    staged_path: str | Path | None = None,
    reference_images=None,
) -> tuple[bytes, dict]:
    if not _lock.acquire(blocking=False):
        raise RuntimeError(
            "[OUT_OF_MEMORY] Un run image est déjà en cours (famille exclusive C07)."
        )
    try:
        seed = _validate_seed(seed)
        return _run_t2i_locked(
            prompt,
            seed,
            width,
            height,
            timeout_s,
            cancel_flag,
            on_stage,
            staged_path,
            mode="create",
            reference_images=reference_images,
        )
    finally:
        _lock.release()


def run_edit(
    prompt: str,
    seed: int | None = None,
    timeout_s: int = RUN_TIMEOUT_S,
    cancel_flag=None,
    on_stage=None,
    *,
    width: int = 512,
    height: int = 512,
    init_image=None,
    mask=None,
    mode: str = "global",
    strength: float = 0.65,
    staged_path: str | Path | None = None,
    source_rect=None,
    reference_images=None,
) -> tuple[bytes, dict]:
    if not init_image:
        raise ValueError("[BAD_ARGS] Mode édition requiert une image initiale.")
    if not _lock.acquire(blocking=False):
        raise RuntimeError(
            "[OUT_OF_MEMORY] Un run image est déjà en cours (famille exclusive C07)."
        )
    try:
        seed = _validate_seed(seed)
        return _run_t2i_locked(
            prompt,
            seed,
            width,
            height,
            timeout_s,
            cancel_flag,
            on_stage,
            staged_path,
            init_image=init_image,
            mask=mask,
            mode=mode,
            strength=strength,
            source_rect=source_rect,
            reference_images=reference_images,
        )
    finally:
        _lock.release()


def composite_protected(
    generated_png: bytes, work_png: bytes, mask_png: bytes
) -> bytes:
    import io as _io
    from PIL import Image as _Image

    generated = _Image.open(_io.BytesIO(generated_png)).convert("RGB").convert("RGBA")
    work = _Image.open(_io.BytesIO(work_png)).convert("RGBA")
    mask = _Image.open(_io.BytesIO(mask_png)).convert("L")
    if work.size != mask.size:
        raise ValueError(
            "[DIMENSIONS_MISMATCH] work and mask must have the same dimensions."
        )
    if generated.size != work.size:
        raise ValueError(
            "[DIMENSIONS_MISMATCH] generated image must match work dimensions."
        )
    out = generated.copy()
    wpx = work.load()
    mpx = mask.load()
    opx = out.load()
    width, height = work.size
    for y in range(height):
        for x in range(width):
            if int(mpx[x, y]) < 128:
                opx[x, y] = wpx[x, y]
    buffer = _io.BytesIO()
    out.save(buffer, format="PNG")
    return buffer.getvalue()


def _provenance(meta: dict, sha: str) -> dict:
    width = meta.get("width")
    height = meta.get("height")
    dimensions = meta.get("dimensions")
    if isinstance(dimensions, dict):
        width = dimensions.get("width", width)
        height = dimensions.get("height", height)
    if dimensions is None and isinstance(width, int) and isinstance(height, int):
        dimensions = {"width": width, "height": height}
    if dimensions is None and isinstance(meta.get("size"), int):
        width = width or meta["size"]
        height = height or meta["size"]
        dimensions = {"width": width, "height": height}
    return {
        "checkpoint": CHECKPOINT,
        "model": meta.get("model") or MODEL_NAME,
        "backend": BACKEND_NAME,
        "prompt": meta.get("prompt"),
        "aspect_ratio": meta.get("aspect_ratio"),
        "seed": meta.get("seed"),
        "width": width,
        "height": height,
        "dimensions": dimensions,
        "run_id": meta.get("run_id"),
        "origin": meta.get("origin"),
        "idempotency_key": meta.get("idempotency_key"),
        "mode": meta.get("mode"),
        "source_artifact_id": meta.get("source_artifact_id"),
        "session_id": meta.get("session_id"),
        "revision": meta.get("revision"),
        "work_sha256": meta.get("work_sha256"),
        "mask_sha256": meta.get("mask_sha256"),
        "mask_kind": meta.get("mask_kind"),
        "reference_sha256": meta.get("reference_sha256"),
        "quality": "experimental",
        "sha256": sha,
    }


def publish_generated(
    db,
    png_bytes: bytes,
    meta: dict,
    scope_conversation_id: str | None = None,
    before_commit=None,
) -> dict:
    import uuid

    from backend.config.paths import get_data_dir
    from backend.db.models import Artifact, ArtifactCard, ArtifactLink, Blob

    width = meta.get("width")
    height = meta.get("height")
    if width is None and height is None and isinstance(meta.get("size"), int):
        width = meta["size"]
        height = meta["size"]
    if isinstance(width, int) and isinstance(height, int):
        _validate_png(png_bytes, width, height)
    sha = hashlib.sha256(png_bytes).hexdigest()
    artifact_id = f"art_{sha[:12]}"
    data_dir = get_data_dir()
    blob_dir = data_dir / "blobs" / "sha256" / sha[:2]
    blob_dir.mkdir(parents=True, exist_ok=True)
    blob_path = blob_dir / f"{sha}.bin"
    if not blob_path.exists():
        blob_path.write_bytes(png_bytes)
    relative_path = f"blobs/sha256/{sha[:2]}/{sha}.bin"
    if not db.query(Blob).filter(Blob.sha256 == sha).first():
        db.add(
            Blob(
                id=sha,
                sha256=sha,
                relative_path=relative_path,
                size_bytes=len(png_bytes),
                mime_detected="image/png",
                state="available",
            )
        )
    artifact = db.query(Artifact).filter(Artifact.id == artifact_id).first()
    if artifact is None:
        name = (meta.get("display_name") or f"flux_{sha[:8]}.png")[:120]
        artifact = Artifact(
            id=artifact_id,
            blob_id=sha,
            kind="image",
            original_filename=name,
            display_name=name,
        )
        db.add(artifact)
    db.flush()
    if (
        not db.query(ArtifactLink)
        .filter(
            ArtifactLink.artifact_id == artifact_id,
            ArtifactLink.scope_kind == "library",
        )
        .first()
    ):
        db.add(
            ArtifactLink(
                id=f"lnk_{artifact_id}_library",
                artifact_id=artifact_id,
                scope_kind="library",
                scope_id="me",
                role="generated",
            )
        )
    if (
        scope_conversation_id
        and not db.query(ArtifactLink)
        .filter(
            ArtifactLink.artifact_id == artifact_id,
            ArtifactLink.scope_kind == "conversation",
            ArtifactLink.scope_id == scope_conversation_id,
        )
        .first()
    ):
        db.add(
            ArtifactLink(
                id=f"lnk_{artifact_id}_{scope_conversation_id}",
                artifact_id=artifact_id,
                scope_kind="conversation",
                scope_id=scope_conversation_id,
                role="generated",
            )
        )
    db.flush()
    current_card = (
        db.query(ArtifactCard)
        .filter(
            ArtifactCard.artifact_id == artifact_id,
            ArtifactCard.is_current == 1,
        )
        .first()
    )
    if current_card is None:
        summary = json.dumps(
            _provenance(meta, sha), ensure_ascii=False, separators=(",", ":")
        )
        db.add(
            ArtifactCard(
                id=f"card_{uuid.uuid4().hex[:12]}",
                artifact_id=artifact_id,
                revision=1,
                is_current=1,
                title=(meta.get("display_name") or "Image Flux")[:120],
                summary=summary,
                analysis_status="generated",
                capabilities_json='["image"]',
            )
        )
    from backend.services.effect_ledger import artifact_event_key, enqueue_event

    enqueue_event(
        db,
        event_key=artifact_event_key(meta.get("run_id"), artifact_id),
        run_id=meta.get("run_id"),
        event_type="artifact_created",
        payload={
            "artifact_id": artifact_id,
            "run_id": meta.get("run_id"),
            "conversation_id": scope_conversation_id,
            "experimental": True,
        },
    )
    if before_commit is not None:
        before_commit(db, artifact_id)
    db.commit()
    return {"artifact_id": artifact_id, "sha256": sha, "size_bytes": len(png_bytes)}
