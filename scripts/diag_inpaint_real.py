"""Diagnostic inpainting réel : trou intérieur source 1024x1024, V4."""

import hashlib
import io
import json
import os
import pathlib
import shutil
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
CAP_ROOT = pathlib.Path("/tmp/opencode/v4-inpaint")
CAP_ROOT.mkdir(parents=True, exist_ok=True)
DATA = CAP_ROOT / "data"
DATA.mkdir(parents=True, exist_ok=True)

RUNS = [
    ("none", "v4-inpaint-default", "default", None),
    ("original", "v4-inpaint-ref", "ref", "original"),
]

os.environ["THOT_DATA_DIR"] = str(DATA)
os.environ["THOT_IMAGE_CAPTURE_DIR"] = str(CAP_ROOT)
os.environ["THOT_IMAGE_MASK_DIAGNOSTICS"] = "1"
# état propre au démarrage
for _k in (
    "THOT_IMAGE_SOURCE_REFERENCE",
    "THOT_IMAGE_KEEP_SOURCE_REFERENCE",
    "THOT_IMAGE_NO_SOURCE_REFERENCE",
):
    os.environ.pop(_k, None)

optin = DATA / "image_backend.optin"
optin.write_text("flux2\n")
os.environ["THOT_IMAGE_OPTIN_PATH"] = str(optin)

from PIL import Image, ImageDraw, ImageFont
import numpy as np

_sys = sys
_pl = pathlib
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parents[1]))

import backend.services.flux_spike as _fx_mod

_current_log = {"path": None}


def _append_all(log_tail, text):
    value = (text or "").strip()
    if value and _current_log["path"] is not None:
        try:
            with _current_log["path"].open("a", encoding="utf-8") as _f:
                _f.write(value + "\n")
        except Exception:
            pass
    return _orig_append(log_tail, text)


_orig_append = _fx_mod._append_log
_fx_mod._append_log = _append_all

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from backend.db.models import Base
import backend.services.image_service as svc

engine = create_engine(f"sqlite:///{DATA / 'diag.db'}")
Base.metadata.create_all(bind=engine)
Session = sessionmaker(
    bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
)

svc.check_install = lambda: (True, "ok")
svc.check_vram = lambda: (True, "ok")
svc._rendre_la_vram_du_worker_ocr = lambda: "ok"

# Source full 1024x1024
source_path = ROOT / "scripts/assets/personne-1.png"
source = Image.open(source_path).convert("RGB")
assert source.size == (1024, 1024), source.size

CW, CH = 1024, 1024
SW, SH = 1024, 1024
HOLE = (352, 256, 672, 576)  # x0,y0,x1,y1
x0, y0, x1, y1 = HOLE

work = source.copy()
mask = Image.new("L", (CW, CH), 0)
mp = mask.load()
for yy in range(CH):
    for xx in range(CW):
        if x0 <= xx < x1 and y0 <= yy < y1:
            mp[xx, yy] = 255
        else:
            mp[xx, yy] = 0

from backend.config.paths import get_data_dir as _gdd

studio = _gdd() / "temp" / "image-studio"
studio.mkdir(parents=True, exist_ok=True)


def stage(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    data = buf.getvalue()
    tok = hashlib.md5(data).hexdigest()
    (studio / f"{tok}.png").write_bytes(data)
    return tok, hashlib.sha256(data).hexdigest()


transform = {"x": 0.0, "y": 0.0, "width": 1024.0, "height": 1024.0}
source_size = {"width": SW, "height": SH}

prompt = "Replace the selected area with a polished chrome robot mask, keeping the same lighting and pose."

source.save(CAP_ROOT / "source.png")
work.save(CAP_ROOT / "work.png")
mask.save(CAP_ROOT / "mask.png")

results = {}
run_logs = {}
for env_mode, run_id, short_dir, env_value in RUNS:
    # env par run
    for _k in (
        "THOT_IMAGE_SOURCE_REFERENCE",
        "THOT_IMAGE_KEEP_SOURCE_REFERENCE",
        "THOT_IMAGE_NO_SOURCE_REFERENCE",
    ):
        os.environ.pop(_k, None)
    if env_value is not None:
        os.environ["THOT_IMAGE_SOURCE_REFERENCE"] = env_value

    cap_dir = CAP_ROOT / run_id
    cap_dir.mkdir(parents=True, exist_ok=True)
    log_path = cap_dir / "worker.log"
    try:
        log_path.unlink()
    except FileNotFoundError:
        pass
    _current_log["path"] = log_path
    # re-stage (tokens consommés par generate)
    work_tok, work_sha = stage(work)
    mask_tok, mask_sha = stage(mask)

    print(f"=== RUN env_mode={env_mode} run_id={run_id} ===", flush=True)
    res = svc.generate(
        None,
        run_id,
        prompt,
        "1:1",
        [],
        1234,
        db_session_factory=Session,
        origin="studio",
        mode="inpaint",
        work_token=work_tok,
        mask_token=mask_tok,
        work_sha256=work_sha,
        mask_sha256=mask_sha,
        canvas_width=CW,
        canvas_height=CH,
        strength=1.0,
        mask_kind="fill",
        source_transform=transform,
        source_size=source_size,
    )
    print(
        "RESULT",
        json.dumps({k: res.get(k) for k in ("artifact_id", "sha256")}),
        flush=True,
    )
    results[env_mode] = (run_id, res)
    run_logs[env_mode] = log_path

    # alias capture dir requested (default/ref)
    alias_dir = CAP_ROOT / short_dir
    if alias_dir.exists():
        shutil.rmtree(alias_dir)
    shutil.copytree(cap_dir, alias_dir)

_current_log["path"] = None


# --- métriques ---
import numpy as np


def load(run_id):
    d = CAP_ROOT / run_id
    final = np.asarray(Image.open(d / "final.png").convert("RGB"), dtype=np.int16)
    work = np.asarray(Image.open(d / "work.png").convert("RGB"), dtype=np.int16)
    mask = np.asarray(Image.open(d / "mask_backend.png").convert("L"), dtype=np.uint8)
    cand = np.asarray(
        Image.open(d / "candidate_before_composite.png").convert("RGB"), dtype=np.int16
    )
    return d, final, work, mask, cand


loaded = {}
for env_mode, run_id, short_dir, env_value in RUNS:
    d, final, work, mask, cand = load(run_id)
    loaded[env_mode] = (run_id, d, final, work, mask, cand)


def hole_stats(final, work):
    diff = np.abs(final[y0:y1, x0:x1, :] - work[y0:y1, x0:x1, :])
    max_chan = diff.max(axis=2)
    n = (y1 - y0) * (x1 - x0)
    changed = int((max_chan > 8).sum())
    changed_ratio = changed / max(1, n)
    mean_abs = float(diff.mean())
    return changed_ratio, mean_abs, changed, n


def protected_stats(final, work, mask):
    prot = mask < 128
    diff = np.abs(final - work)
    max_chan = diff.max(axis=2)
    if not prot.any():
        return 0, 0, 0
    max_delta = int(diff[prot].max())
    changed_pixels = int(((prot) & (max_chan > 0)).sum())
    return max_delta, changed_pixels, int(prot.sum())


def seam_stats(final, x0, y0, x1, y1):
    a = final.astype(np.float64)
    # seam pairs (outside,inside)
    pairs = []
    # top: (y0-1, x) vs (y0, x)
    pairs.append((a[y0 - 1, x0:x1, :], a[y0, x0:x1, :]))
    # bottom: (y1, x) vs (y1-1, x)
    pairs.append((a[y1, x0:x1, :], a[y1 - 1, x0:x1, :]))
    # left: (y, x0-1) vs (y, x0)
    pairs.append((a[y0:y1, x0 - 1, :], a[y0:y1, x0, :]))
    # right: (y, x1) vs (y, x1-1)
    pairs.append((a[y0:y1, x1, :], a[y0:y1, x1 - 1, :]))
    seam_diffs = []
    for p_out, p_in in pairs:
        seam_diffs.append(np.abs(p_out - p_in))
    seam_mean = float(
        np.mean(np.concatenate([d.reshape(-1, 3) for d in seam_diffs], axis=0))
    )

    # base: adjacent pairs just inside hole along inward normal
    base_diffs = []
    # top inward: row y0 vs y0+1
    base_diffs.append(np.abs(a[y0, x0:x1, :] - a[y0 + 1, x0:x1, :]))
    # bottom inward: row y1-2 vs y1-1
    base_diffs.append(np.abs(a[y1 - 2, x0:x1, :] - a[y1 - 1, x0:x1, :]))
    # left inward: col x0 vs x0+1
    base_diffs.append(np.abs(a[y0:y1, x0, :] - a[y0:y1, x0 + 1, :]))
    # right inward: col x1-2 vs x1-1
    base_diffs.append(np.abs(a[y0:y1, x1 - 2, :] - a[y0:y1, x1 - 1, :]))
    base_mean = float(
        np.mean(np.concatenate([d.reshape(-1, 3) for d in base_diffs], axis=0))
    )
    ratio = seam_mean / base_mean if base_mean > 0 else None
    return seam_mean, base_mean, ratio


metrics = {}
for env_mode, run_id, short_dir, env_value in RUNS:
    _, d, final, work, mask, cand = loaded[env_mode]
    changed_ratio, mean_abs, changed, n = hole_stats(final, work)
    prot_max, prot_changed, prot_n = protected_stats(final, work, mask)
    seam_mean, base_mean, seam_ratio = seam_stats(final, x0, y0, x1, y1)

    # manifest
    import json as _j

    manifest = _j.loads((d / "manifest.json").read_text())
    from backend.services.capture_manifest import manifest_valid

    mv, reasons = manifest_valid(manifest, d)

    # worker log source ref
    log_lines = (
        (d / "worker.log").read_text().splitlines()
        if (d / "worker.log").is_file()
        else []
    )
    src_ref_lines = [ln for ln in log_lines if "[SOURCE_REF]" in ln]

    metrics[env_mode if False else run_id] = {
        "run_id": run_id,
        "short_dir": short_dir,
        "capture_dir": str(d),
        "alias_dir": str(CAP_ROOT / short_dir),
        "env_value": env_value,
        "source_reference_manifest": manifest.get("inference", {}).get(
            "source_reference"
        ),
        "source_ref_log_lines": src_ref_lines,
        "manifest_valid": bool(mv),
        "manifest_reasons": reasons,
        "capture_status": manifest.get("capture_status"),
        "steps_executed": manifest.get("inference", {}).get("steps_executed"),
        "mode": manifest.get("mode"),
        "mask_kind": manifest.get("mask_kind"),
        "strength": manifest.get("strength"),
        "hole": {
            "changed_ratio_final_vs_work": changed_ratio,
            "changed_pixels": changed,
            "total_pixels": n,
            "mean_abs_diff_final_vs_work": mean_abs,
        },
        "protected": {
            "max_delta": prot_max,
            "changed_pixels": prot_changed,
            "protected_pixels": prot_n,
        },
        "seam": {
            "seam_mean": seam_mean,
            "base_mean": base_mean,
            "seam_ratio": seam_ratio,
        },
    }

# A vs B comparison in hole
a_run = "v4-inpaint-default"
b_run = "v4-inpaint-ref"
_, _, finalA, workA, _, _ = loaded["none"]
_, _, finalB, workB, _, _ = loaded["original"]
diffAB = np.abs(finalA[y0:y1, x0:x1, :] - finalB[y0:y1, x0:x1, :])
metrics["comparison_A_vs_B_hole"] = {
    "mean_abs_diff_finalA_vs_finalB": float(diffAB.mean()),
    "changed_ratio_gt8": float((diffAB.max(axis=2) > 8).mean()),
}

# add worker log source ref print for task verification
for env_mode, run_id, short_dir, env_value in RUNS:
    mkey = run_id
    print(
        f"RUN {run_id} source_reference={metrics[mkey]['source_reference_manifest']}",
        flush=True,
    )
    print(
        f"RUN {run_id} changed_ratio={metrics[mkey]['hole']['changed_ratio_final_vs_work']}",
        flush=True,
    )
    print(
        f"RUN {run_id} mean_abs_diff={metrics[mkey]['hole']['mean_abs_diff_final_vs_work']}",
        flush=True,
    )
    print(
        f"RUN {run_id} protected_max_delta={metrics[mkey]['protected']['max_delta']}",
        flush=True,
    )
    print(
        f"RUN {run_id} seam_mean={metrics[mkey]['seam']['seam_mean']} base={metrics[mkey]['seam']['base_mean']} ratio={metrics[mkey]['seam']['seam_ratio']}",
        flush=True,
    )
    print(
        f"RUN {run_id} manifest_valid={metrics[mkey]['manifest_valid']} steps={metrics[mkey]['steps_executed']}",
        flush=True,
    )
    for ln in metrics[mkey]["source_ref_log_lines"]:
        print(ln, flush=True)

with open(CAP_ROOT / "metrics.json", "w", encoding="utf-8") as f:
    json.dump(metrics, f, indent=2, ensure_ascii=False)
print("METRICS_JSON", json.dumps(metrics, indent=2, ensure_ascii=False), flush=True)

# --- montage compare.png ---
src_img = source
finalA_img = Image.open(CAP_ROOT / "v4-inpaint-default" / "final.png").convert("RGB")
finalB_img = Image.open(CAP_ROOT / "v4-inpaint-ref" / "final.png").convert("RGB")

canvas = Image.new("RGB", (CW * 3, CH), (0, 0, 0))
canvas.paste(src_img, (0, 0))
canvas.paste(finalA_img, (CW, 0))
canvas.paste(finalB_img, (CW * 2, 0))

draw = ImageDraw.Draw(canvas)
try:
    font = ImageFont.truetype(
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 24
    )
except Exception:
    font = ImageFont.load_default()
# top banner
draw.rectangle([0, 0, CW * 3, 42], fill=(0, 0, 0))
labels = [
    (CW // 2, "SOURCE (personne-1)"),
    (CW + CW // 2, "A  v4-inpaint-default  (ref=none)"),
    (CW * 2 + CW // 2, "B  v4-inpaint-ref  (ref=original)"),
]
for cx, txt in labels:
    bbox = draw.textbbox((0, 0), txt, font=font)
    tw = bbox[2] - bbox[0]
    draw.text((cx - tw // 2, 8), txt, fill=(255, 255, 255), font=font)

canvas.save(CAP_ROOT / "compare.png")
print("COMPARE saved", str(CAP_ROOT / "compare.png"), flush=True)

# also save work/mask copies at root if needed
print("DONE", flush=True)
