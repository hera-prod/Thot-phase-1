"""Vérification sans GPU des captures V4 existantes.

Pour chaque run demandé, valide le manifeste et vérifie la préservation des
pixels protégés via le guard RGBA.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def diagnostic_root() -> Path:
    override = os.environ.get("THOT_DIAG_ROOT")
    return (
        Path(override or (Path(tempfile.gettempdir()) / "thot-diag"))
        .expanduser()
        .resolve()
    )


def capture_parent(root: Path, folder: str) -> Path:
    override = os.environ.get("THOT_IMAGE_CAPTURE_DIR")
    return Path(override or (root / folder)).expanduser().resolve()


def capture_run_dirs(
    root: Path, capture_set: str, *, include_baseline: bool = False
) -> list[Path]:
    suffix = "noref" if capture_set == "historical" else "noref-src"
    runs = [
        capture_parent(root, "v4-captures-noref") / f"v4-o2-{suffix}",
        capture_parent(root, "v4-captures-o1") / f"v4-o1-{suffix}",
        capture_parent(root, "v4-captures-i1") / f"v4-i1-{suffix}",
    ]
    if include_baseline:
        runs.insert(0, capture_parent(root, "v4-captures") / "v4-o2-real")
    return runs


def capture_argument_parser(description: str | None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=description,
        epilog="Racine : THOT_DIAG_ROOT (défaut : thot-diag dans le dossier temporaire). "
        "THOT_IMAGE_CAPTURE_DIR permet de lire tous les runs depuis un même dossier.",
    )
    parser.add_argument(
        "--capture-set",
        choices=("source", "historical"),
        default="source",
        help="source : runs produits actuellement (*-noref-src, défaut) ; "
        "historical : anciennes captures (*-noref), sans repli automatique.",
    )
    return parser


def require_capture_files(run_dir: Path, *names: str) -> None:
    missing = [name for name in names if not (run_dir / name).is_file()]
    if missing:
        raise FileNotFoundError(
            f"Capture requise dans {run_dir} : fichiers absents : {', '.join(missing)}"
        )


METRICS_CAPTURE_FILES = (
    "manifest.json",
    "work.png",
    "candidate_before_composite.png",
    "final.png",
    "mask_backend.png",
)


def prepare_capture_producer(folder: str, run_id: str) -> tuple[Path, Path, Path]:
    root = diagnostic_root()
    data = (
        Path(os.environ.get("THOT_DATA_DIR") or (root / "v4-data"))
        .expanduser()
        .resolve()
    )
    captures = capture_parent(root, folder)
    inputs = root / "v4-inputs" / run_id
    for directory in (data, captures, inputs):
        directory.mkdir(parents=True, exist_ok=True)
    optin = data / "image_backend.optin"
    optin.write_text("flux2\n", encoding="utf-8")
    os.environ.update(
        THOT_DATA_DIR=str(data),
        THOT_IMAGE_CAPTURE_DIR=str(captures),
        THOT_IMAGE_MASK_DIAGNOSTICS="1",
        THOT_IMAGE_OPTIN_PATH=str(optin),
    )
    return data, captures / run_id, inputs


def load_produced_manifest(run_dir: Path) -> dict:
    try:
        require_capture_files(run_dir, *METRICS_CAPTURE_FILES)
        return json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Capture illisible dans {run_dir} : {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    args = capture_argument_parser(__doc__).parse_args(argv)

    from backend.image_engine.composite_guard import assert_protected_preserved
    from backend.services.capture_manifest import manifest_valid

    failed = False
    for run_dir in capture_run_dirs(diagnostic_root(), args.capture_set):
        manifest_path = run_dir / "manifest.json"
        try:
            require_capture_files(
                run_dir, "manifest.json", "final.png", "work.png", "mask_backend.png"
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"{run_dir} manifest_valid=False ({exc}) leak=NA", file=sys.stderr)
            failed = True
            continue

        ok, reasons = manifest_valid(manifest, run_dir)
        mv = "True" if ok else f"False({','.join(reasons)})"

        final_p = run_dir / "final.png"
        work_p = run_dir / "work.png"
        mask_p = run_dir / "mask_backend.png"
        try:
            assert_protected_preserved(
                final_p.read_bytes(), work_p.read_bytes(), mask_p.read_bytes()
            )
            leak = "ok"
        except RuntimeError as exc:
            # Diagnostic utile : le message contient déjà le compteur
            # ([COMPOSITE_LEAK] N pixels protégés modifiés). On ne parse pas le
            # message ; on le rapporte tel quel et l'échec est conservé.
            leak = str(exc)
        except (OSError, ValueError) as exc:
            leak = f"error:{exc}"

        print(f"{run_dir} manifest_valid={mv} leak={leak}")
        failed |= not ok or leak != "ok"
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
