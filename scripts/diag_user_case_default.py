"""Diagnostic cas utilisateur exact : défaut (source_reference=original sans env)."""

import hashlib
import io
import json
import os
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
CAP_ROOT = pathlib.Path("/tmp/opencode")
CAP_ROOT.mkdir(parents=True, exist_ok=True)
RUN_ID = "v4-outpaint-default"
CAP_DIR = CAP_ROOT / RUN_ID
CAP_DIR.mkdir(parents=True, exist_ok=True)

DATA = CAP_DIR / "data"
DATA.mkdir(parents=True, exist_ok=True)
os.environ["THOT_DATA_DIR"] = str(DATA)
os.environ["THOT_IMAGE_CAPTURE_DIR"] = str(CAP_ROOT)
os.environ["THOT_IMAGE_MASK_DIAGNOSTICS"] = "1"
for _k in (
    "THOT_IMAGE_SOURCE_REFERENCE",
    "THOT_IMAGE_KEEP_SOURCE_REFERENCE",
    "THOT_IMAGE_NO_SOURCE_REFERENCE",
):
    os.environ.pop(_k, None)
optin = DATA / "image_backend.optin"
optin.write_text("flux2\n")
os.environ["THOT_IMAGE_OPTIN_PATH"] = str(optin)

RUN_WORKER_LOG = CAP_DIR / "worker.log"
try:
    RUN_WORKER_LOG.unlink()
except FileNotFoundError:
    pass

from PIL import Image
import numpy as np

_sys = sys
_pl = pathlib
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parents[1]))

import backend.services.flux_spike as _fx_mod

_orig_append = _fx_mod._append_log


def _append_all(log_tail, text):
    value = (text or "").strip()
    if value:
        try:
            with RUN_WORKER_LOG.open("a", encoding="utf-8") as _f:
                _f.write(value + "\n")
        except Exception:
            pass
    return _orig_append(log_tail, text)


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

repo_db = ROOT / "data" / "database" / "thot-phase-1.db"
rconn = sqlite3.connect(f"file:{repo_db}?mode=ro", uri=True)
rcur = rconn.cursor()
bid = rcur.execute(
    "SELECT blob_id FROM artifacts WHERE id='art_795c1f9294d6'"
).fetchone()[0]
rel = rcur.execute("SELECT relative_path FROM blobs WHERE id=?", (bid,)).fetchone()[0]
rconn.close()
source_bytes = (ROOT / "data" / rel).read_bytes()
source = Image.open(io.BytesIO(source_bytes)).convert("RGB")
assert source.size == (1024, 1024), source.size

CW, CH = 1568, 672
RX, RY, RW, RH = 448, 0, 672, 672

work = Image.new("RGB", (CW, CH), (0, 0, 0))
small = source.resize((RW, RH), Image.LANCZOS)
work.paste(small, (RX, RY))
px = work.load()
for y in range(CH):
    for x in range(CW):
        inside = RX <= x < RX + RW and RY <= y < RY + RH
        if inside:
            continue
        sx = min(max(x, RX), RX + RW - 1)
        sy = min(max(y, RY), RY + RH - 1)
        px[x, y] = px[sx, sy]

mask = Image.new("L", (CW, CH), 0)
mp = mask.load()
for y in range(CH):
    for x in range(CW):
        inside = RX <= x < RX + RW and RY <= y < RY + RH
        mp[x, y] = 0 if inside else 255

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


prompt = "Reconstruct and extend naturally the masked area surrounding the scene."
transform = {"x": RX, "y": RY, "width": RW, "height": RH}
source_size = {"width": 1024, "height": 1024}

work_tok, work_sha = stage(work)
mask_tok, mask_sha = stage(mask)
print(f"=== DEFAULT (no env) run_id={RUN_ID} ===", flush=True)
res = svc.generate(
    None,
    RUN_ID,
    prompt,
    aspect_ratio="21:9",
    seed=2523272678,
    db_session_factory=Session,
    origin="studio",
    mode="outpaint",
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
    "RESULT", json.dumps({k: res.get(k) for k in ("artifact_id", "sha256")}), flush=True
)

work.save(str(CAP_DIR / "work_local.png"))

final_path = CAP_DIR / "final.png"
assert final_path.is_file(), final_path
final = Image.open(final_path).convert("RGB")
assert final.size == (CW, CH), final.size
final_arr = np.asarray(final).astype(np.int16)
work_arr = np.asarray(work).astype(np.int16)

prot = final_arr[:, RX : RX + RW, :]
prot_work = work_arr[:, RX : RX + RW, :]
diff_prot = np.abs(prot - prot_work)
max_delta_prot = int(diff_prot.max()) if diff_prot.size else 0
num_changed_prot = int(np.count_nonzero(np.any(diff_prot != 0, axis=2)))
print(
    f"PROTECTED max_delta={max_delta_prot} changed_pixels={num_changed_prot}",
    flush=True,
)

left_seam = float(np.mean(np.abs(final_arr[:, 448, :] - final_arr[:, 447, :])))
right_seam = float(np.mean(np.abs(final_arr[:, 1120, :] - final_arr[:, 1119, :])))
center_diffs = np.abs(final_arr[:, 500:1100, :] - final_arr[:, 501:1101, :])
base = float(np.mean(center_diffs))
ratio_left = left_seam / base if base > 0 else None
ratio_right = right_seam / base if base > 0 else None
print(
    f"SEAM left={left_seam:.6f} right={right_seam:.6f} base={base:.6f} "
    f"ratio_left={ratio_left:.6f} ratio_right={ratio_right:.6f}",
    flush=True,
)

b_path = pathlib.Path("/tmp/opencode/outpaint-ab/v4-outpaint-original/final.png")
comparison = None
if b_path.is_file():
    b = Image.open(b_path).convert("RGB")
    b_arr = np.asarray(b).astype(np.int16)
    diff_all = np.abs(final_arr - b_arr)
    g = float(diff_all.mean())
    lm = float(diff_all[:, 0:448, :].mean())
    rm = float(diff_all[:, 1120:1568, :].mean())
    comparison = {
        "global_mean_abs": g,
        "left_margin_mean_abs": lm,
        "right_margin_mean_abs": rm,
    }
    print(
        f"COMPARE_TO_B global={g:.6f} left_margin={lm:.6f} right_margin={rm:.6f}",
        flush=True,
    )
else:
    print("COMPARE_TO_B final.png missing at", b_path, flush=True)

from backend.services.capture_manifest import manifest_valid

manifest_path = CAP_DIR / "manifest.json"
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
mv, reasons = manifest_valid(manifest, CAP_DIR)
print(f"MANIFEST manifest_valid={mv} reasons={reasons}", flush=True)
print(f"MANIFEST capture_status={manifest.get('capture_status')}", flush=True)
print(
    f"MANIFEST steps_executed={manifest.get('inference', {}).get('steps_executed')}",
    flush=True,
)
print(
    f"MANIFEST source_reference={manifest.get('inference', {}).get('source_reference')}",
    flush=True,
)
print(f"MANIFEST transform={manifest.get('source', {}).get('transform')}", flush=True)

stats = res.get("stats") or {}
print("STATS_STEPS", stats.get("steps_executed"), flush=True)
src_ref_lines = [
    ln
    for ln in RUN_WORKER_LOG.read_text(encoding="utf-8").splitlines()
    if "[SOURCE_REF]" in ln
]
print("SOURCE_REF_LINES:")
for ln in src_ref_lines:
    print(ln, flush=True)

summary = {
    "run_id": RUN_ID,
    "capture_dir": str(CAP_DIR),
    "manifest_valid": mv,
    "manifest_reasons": reasons,
    "capture_status": manifest.get("capture_status"),
    "steps_executed": manifest.get("inference", {}).get("steps_executed"),
    "source_reference": manifest.get("inference", {}).get("source_reference"),
    "source_transform": manifest.get("source", {}).get("transform"),
    "protected_max_delta": max_delta_prot,
    "protected_changed_pixels": num_changed_prot,
    "seam_left": left_seam,
    "seam_right": right_seam,
    "seam_base": base,
    "seam_ratio_left": ratio_left,
    "seam_ratio_right": ratio_right,
    "comparison_to_B": comparison,
}
with open(CAP_DIR / "diag_summary.json", "w", encoding="utf-8") as f:
    json.dump(summary, f, indent=2, ensure_ascii=False)
print("SUMMARY_JSON", json.dumps(summary, ensure_ascii=False), flush=True)
