"""Check that all model GGUF files referenced in MODEL_CONFIGS exist on disk."""

import logging
import os
from pathlib import Path

from backend.config.defaults import MODEL_CONFIGS

# Resolve to Thot-phase-1 directory (parent of backend/)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


log = logging.getLogger(__name__)


def _size_bytes(path: Path) -> int:
    return path.stat().st_size


def check_models() -> dict:
    """
    Verify every model GGUF (and optional mmproj) exists and log its size.

    Returns
    -------
    summary : dict[str, dict]
        Mapping of model name to:
            {
                "path_ok": bool,
                "mmproj_ok": bool,
                "path_size_mb": float,
            }
        ``mmproj_ok`` is ``True`` when there is no mmproj entry (skip),
        and ``path_size_mb`` is ``0.0`` when the main GGUF is missing.

    Raises
    ------
    RuntimeError
        If any required file is missing, with a message listing exactly
        which files are absent and where to obtain them.
    """
    summary: dict[str, dict] = {}
    missing: list[str] = []

    for name, cfg in MODEL_CONFIGS.items():
        entry: dict = {"path_ok": False, "mmproj_ok": True, "path_size_mb": 0.0}

        # ── main GGUF path (relative to $PROJECT_ROOT → absolute) ──
        main_path = _PROJECT_ROOT / cfg.path
        log.info("Checking model %s → %s", name, main_path)

        if main_path.is_file():
            size = _size_bytes(main_path)
            entry["path_ok"] = True
            entry["path_size_mb"] = round(size / (1024 * 1024), 2)
            log.info("  ✓ main GGUF  %.2f MB", entry["path_size_mb"])
        else:
            entry["path_ok"] = False
            entry["path_size_mb"] = 0.0
            missing.append(f"{name}: {cfg.path} (main GGUF)")
            log.error("  ✗ MISSING  %s", main_path)

        # ── optional mmproj ──
        if cfg.mmproj is not None:
            mmproj_path = _PROJECT_ROOT / cfg.mmproj
            log.info("  Checking mmproj → %s", mmproj_path)

            if mmproj_path.is_file():
                mm_size = _size_bytes(mmproj_path)
                entry["mmproj_ok"] = True
                log.info("  ✓ mmproj     %.2f MB", round(mm_size / (1024 * 1024), 2))
            else:
                entry["mmproj_ok"] = False
                missing.append(f"{name}: {cfg.mmproj} (mmproj)")
                log.error("  ✗ MISSING  %s", mmproj_path)

        summary[name] = entry

    # ── fail fast when something is absent ──
    if missing:
        lines = "\n".join("  - " + m for m in missing)
        raise RuntimeError(
            "One or more model files are missing:\n"
            f"{lines}\n\n"
            "Place the files at the expected locations or update\n"
            f"MODEL_CONFIGS in {os.path.join(str(_PROJECT_ROOT), 'backend', 'config', 'defaults.py')}."
        )

    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = check_models()
    print(result)
