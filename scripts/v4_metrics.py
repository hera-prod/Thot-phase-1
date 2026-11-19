#!/usr/bin/env python3
"""Métriques exactes sur les captures V4 existantes, sans GPU."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.check_captures_v4 import (
    METRICS_CAPTURE_FILES,
    capture_argument_parser,
    capture_run_dirs,
    diagnostic_root,
    require_capture_files,
)

REGION_NAMES = ["top", "bottom", "left", "right", "center"]


def load_images(run_dir: Path) -> dict[str, np.ndarray]:
    work = np.asarray(Image.open(run_dir / "work.png").convert("RGB"), dtype=np.int16)
    cand = np.asarray(
        Image.open(run_dir / "candidate_before_composite.png").convert("RGB"),
        dtype=np.int16,
    )
    final = np.asarray(Image.open(run_dir / "final.png").convert("RGB"), dtype=np.int16)
    mask = np.asarray(
        Image.open(run_dir / "mask_backend.png").convert("L"), dtype=np.uint8
    )
    if (
        work.shape != cand.shape
        or work.shape != final.shape
        or work.shape[:2] != mask.shape
    ):
        raise ValueError(f"Shape mismatch in {run_dir}")
    return {"work": work, "candidate": cand, "final": final, "mask": mask}


def region_labels(height: int, width: int, transform: dict) -> np.ndarray:
    tx = float(transform["x"])
    ty = float(transform["y"])
    tw = float(transform["width"])
    th = float(transform["height"])
    ys = np.arange(height, dtype=np.float64)[:, None]
    xs = np.arange(width, dtype=np.float64)[None, :]

    labels = np.full((height, width), "center", dtype="<U8")
    top = ys < ty
    bottom = ys >= ty + th
    left = xs < tx
    right = xs >= tx + tw

    middle = ~(top | bottom)

    labels = np.where(top, "top", labels)
    labels = np.where(bottom, "bottom", labels)
    labels = np.where(middle & left, "left", labels)
    labels = np.where(middle & right, "right", labels)
    return labels


def editable_stats(
    diff: np.ndarray, max_chan: np.ndarray, editable: np.ndarray
) -> dict:
    n = int(editable.sum())
    if n == 0 or not editable.any():
        return {"n": 0, "changed_gt8": 0, "changed_ratio": 0.0, "mean_abs_diff": 0.0}
    changed = int(((editable) & (max_chan > 8)).sum())
    mean_abs = float(diff[editable].mean())
    return {
        "n": n,
        "changed_gt8": changed,
        "changed_ratio": round(changed / n, 4),
        "mean_abs_diff": round(mean_abs, 4),
    }


def region_stats(
    images: dict[str, np.ndarray], labels: np.ndarray, region: str
) -> dict:
    work = images["work"]
    cand = images["candidate"]
    final = images["final"]
    mask = images["mask"]
    height, width = mask.shape

    if region == "global_editable":
        base = np.ones((height, width), dtype=bool)
    else:
        base = labels == region

    editable = base & (mask >= 128)

    diff_cw = np.abs(cand - work)
    diff_fw = np.abs(final - work)
    max_cw = diff_cw.max(axis=2)
    max_fw = diff_fw.max(axis=2)

    return {
        "candidate_vs_work": editable_stats(diff_cw, max_cw, editable),
        "final_vs_work": editable_stats(diff_fw, max_fw, editable),
    }


def protected_stats(images: dict[str, np.ndarray]) -> dict[str, dict]:
    work = images["work"]
    cand = images["candidate"]
    final = images["final"]
    mask = images["mask"]
    protected = mask < 128

    def one(img: np.ndarray) -> dict:
        if not protected.any():
            return {"protected_max_delta": 0, "protected_changed_pixels": 0}
        diff = np.abs(img - work)
        max_chan = diff.max(axis=2)
        max_delta = int(diff[protected].max())
        changed_pixels = int(((protected) & (max_chan > 0)).sum())
        return {
            "protected_max_delta": max_delta,
            "protected_changed_pixels": changed_pixels,
        }

    return {
        "candidate_vs_work": one(cand),
        "final_vs_work": one(final),
    }


def before_after(
    img_before: dict, img_after: dict, labels_before: np.ndarray, region: str
) -> dict:
    res: dict[str, dict] = {}
    for key in ("candidate", "final"):
        base = labels_before == region
        editable = base & (img_before["mask"] >= 128) & (img_after["mask"] >= 128)
        n = int(editable.sum())
        diff = np.abs(img_before[key] - img_after[key])
        max_arr = diff.max(axis=2)
        if n == 0 or not editable.any():
            res[key] = {
                "n": 0,
                "changed_gt8": 0,
                "changed_ratio": 0.0,
                "mean_abs_diff": 0.0,
            }
            continue
        changed = int(((editable) & (max_arr > 8)).sum())
        mean_abs = float(diff[editable].mean())
        res[key] = {
            "n": n,
            "changed_gt8": changed,
            "changed_ratio": round(changed / n, 4),
            "mean_abs_diff": round(mean_abs, 4),
        }
    return res


def main(argv: list[str] | None = None) -> int:
    parser = capture_argument_parser(__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        help="Rapport JSON (défaut : v4_metrics.json sous THOT_DIAG_ROOT).",
    )
    args = parser.parse_args(argv)
    root = diagnostic_root()
    runs = capture_run_dirs(root, args.capture_set, include_baseline=True)
    missing_folders: list[str] = []
    output: dict = {
        "capture_set": args.capture_set,
        "runs": {},
        "missing_folders": missing_folders,
    }

    loaded: dict[str, tuple[dict[str, np.ndarray], np.ndarray]] = {}

    for run_dir in runs:
        name = run_dir.name
        try:
            require_capture_files(run_dir, *METRICS_CAPTURE_FILES)
            manifest = json.loads(
                (run_dir / "manifest.json").read_text(encoding="utf-8")
            )
            images = load_images(run_dir)
            transform = manifest["source"]["transform"]
            source_size = manifest["source"]["source_size"]
            steps = manifest["inference"]["steps_executed"]
            height, width = images["mask"].shape
            labels = region_labels(height, width, transform)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            missing_folders.append(str(run_dir))
            output["runs"][name] = {
                "folder": str(run_dir),
                "status": "unavailable",
                "error": str(exc),
            }
            continue

        regions_out: dict[str, dict] = {}
        for r in REGION_NAMES:
            regions_out[r] = region_stats(images, labels, r)
        regions_out["global_editable"] = region_stats(images, labels, "global_editable")

        prot = protected_stats(images)

        run_out = {
            "folder": str(run_dir),
            "status": "present",
            "manifest": {
                "run_id": manifest.get("run_id"),
                "mode": manifest.get("mode"),
                "mask_kind": manifest.get("mask_kind"),
                "strength": manifest.get("strength"),
                "origin": manifest.get("origin"),
                "run_type": manifest.get("run_type"),
                "capture_status": manifest.get("capture_status"),
                "inference_steps_executed": steps,
                "source_transform": transform,
                "source_size": source_size,
            },
            "regions": regions_out,
            "protected": prot,
        }
        output["runs"][name] = run_out
        loaded[name] = (images, labels)

    before_name, after_name = runs[0].name, runs[1].name
    if before_name in loaded and after_name in loaded:
        img_before, labels_before = loaded[before_name]
        img_after, _ = loaded[after_name]
        if img_before["mask"].shape != img_after["mask"].shape:
            output["comparison_error"] = (
                f"Dimensions incompatibles entre {before_name} et {after_name}"
            )
            print(output["comparison_error"], file=sys.stderr)
        else:
            output["o2_before_after"] = {
                "before_run": before_name,
                "after_run": after_name,
                "by_region": {
                    region: before_after(img_before, img_after, labels_before, region)
                    for region in ("left", "right")
                },
                "note": "difference between before and after run images (candidate and final) on editable pixels of left/right regions",
            }

    out_path = args.output or (root / "v4_metrics.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    print("=" * 108)
    print(
        "V4 CAPTURES METRICS (editable pixels mask>=128; changed if channel-max |delta| > 8)"
    )
    print("=" * 108)

    for run_dir in runs:
        name = run_dir.name
        run = output["runs"][name]
        if run["status"] != "present":
            print(f"\n[{name}] {run_dir}: {run['error']}", file=sys.stderr)
            continue
        man = run["manifest"]
        print(f"\n### {name}  ({run_dir})")
        print(
            f"    run_id={man['run_id']} mode={man['mode']} mask_kind={man['mask_kind']} "
            f"strength={man['strength']} origin={man['origin']} run_type={man['run_type']} "
            f"capture_status={man['capture_status']} steps={man['inference_steps_executed']}"
        )
        print(f"    transform={man['source_transform']}")
        print(
            "    region              n      cand->work changed_gt8  ratio   mean_abs | final->work changed_gt8  ratio   mean_abs"
        )
        for key in REGION_NAMES + ["global_editable"]:
            r = run["regions"][key]
            cw = r["candidate_vs_work"]
            fw = r["final_vs_work"]
            label = key if key != "global_editable" else "GLOBAL (editable)"
            print(
                f"    {label:<18} {cw['n']:>7}  "
                f"{cw['changed_gt8']:>10} {cw['changed_ratio']:>6.4f} {cw['mean_abs_diff']:>8.4f} | "
                f"{fw['changed_gt8']:>10} {fw['changed_ratio']:>6.4f} {fw['mean_abs_diff']:>8.4f}"
            )
        prot = run["protected"]
        print(
            f"    protected(mask<128): candidate_vs_work max_delta={prot['candidate_vs_work']['protected_max_delta']} "
            f"changed_pixels={prot['candidate_vs_work']['protected_changed_pixels']} ; "
            f"final_vs_work max_delta={prot['final_vs_work']['protected_max_delta']} "
            f"changed_pixels={prot['final_vs_work']['protected_changed_pixels']}"
        )

    if "o2_before_after" in output:
        print("\n" + "=" * 108)
        print(
            f"O2 BEFORE({before_name}) vs AFTER({after_name}) on left/right editable pixels"
        )
        print("=" * 108)
        comp = output["o2_before_after"]["by_region"]
        for region in ("left", "right"):
            print(f"  {region}:")
            for key in ("candidate", "final"):
                m = comp[region][key]
                print(
                    f"    {key}: n={m['n']} changed_gt8={m['changed_gt8']} "
                    f"changed_ratio={m['changed_ratio']:.4f} mean_abs_diff={m['mean_abs_diff']:.4f}"
                )

    print("\nMissing folders:")
    if missing_folders:
        for p in missing_folders:
            print("  - " + p)
    else:
        print("  (none)")

    print(f"\nJSON written: {out_path}")
    return int(bool(missing_folders) or "comparison_error" in output)


if __name__ == "__main__":
    raise SystemExit(main())
