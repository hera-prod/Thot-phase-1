"""Diagnostic A/B outpainting large avec référence source conditionnée."""

import hashlib
import io
import json
import os
import pathlib
import sqlite3
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
DATA = pathlib.Path("/tmp/opencode/outpaint-ab/data")
DATA.mkdir(parents=True, exist_ok=True)
os.environ["THOT_DATA_DIR"] = str(DATA)
CAP_BASE = pathlib.Path("/tmp/opencode/outpaint-ab")
CAP_BASE.mkdir(parents=True, exist_ok=True)
os.environ["THOT_IMAGE_CAPTURE_DIR"] = str(CAP_BASE)
os.environ["THOT_IMAGE_MASK_DIAGNOSTICS"] = "1"
os.environ.pop("THOT_IMAGE_KEEP_SOURCE_REFERENCE", None)
os.environ.pop("THOT_IMAGE_NO_SOURCE_REFERENCE", None)
optin = DATA / "image_backend.optin"
optin.write_text("flux2\n")
os.environ["THOT_IMAGE_OPTIN_PATH"] = str(optin)

from PIL import Image
import numpy as np

_sys = sys
_pl = pathlib
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parents[1]))

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

# --- source portrait from repo DB (read-only) ---
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

# --- work : source resized to 672x672 pasted at (448,0), then edge extend ---
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

# --- mask : 255 outside rect, 0 inside ---
mask = Image.new("L", (CW, CH), 0)
mp = mask.load()
for y in range(CH):
    for x in range(CW):
        inside = RX <= x < RX + RW and RY <= y < RY + RH
        mp[x, y] = 0 if inside else 255

# --- staging tokens 32 hex ---
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

conds = [
    ("none", "v4-outpaint-none"),
    ("original", "v4-outpaint-original"),
    ("work", "v4-outpaint-work"),
]

results = {}
for mode, run_id in conds:
    os.environ["THOT_IMAGE_SOURCE_REFERENCE"] = mode
    work_tok, work_sha = stage(work)
    mask_tok, mask_sha = stage(mask)
    print(f"=== CONDITION {mode} run_id={run_id} ===", flush=True)
    res = svc.generate(
        None,
        run_id,
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
        "RESULT",
        json.dumps({k: res.get(k) for k in ("artifact_id", "sha256")}),
        flush=True,
    )
    results[mode] = res

# Save work for measurements
work.save("/tmp/opencode/outpaint-ab/work.png")
mask.save("/tmp/opencode/outpaint-ab/mask.png")
source.save("/tmp/opencode/outpaint-ab/source.png")


# --- measurements ---
def load_final(cond_dir_name):
    # capture dir naming from CAP_BASE/env + run_id
    run_id = dict(conds)[cond_dir_name] if False else None
    return None


runid_by_mode = {m: rid for m, rid in conds}
imgs = {}
for mode, rid in conds:
    p = CAP_BASE / rid / "final.png"
    imgs[mode] = Image.open(p).convert("RGB")

work_arr = np.asarray(work).astype(np.float32)


def seam_means(mode):
    a = np.asarray(imgs[mode]).astype(np.float32)
    left_seam = np.mean(np.abs(a[:, 448, :] - a[:, 447, :]))
    right_seam = np.mean(np.abs(a[:, 1120, :] - a[:, 1119, :]))
    # baseline: adjacent cols within center x in [500,1100] (pairs 500..1099)
    diffs = np.abs(a[:, 500:1100, :] - a[:, 501:1101, :])
    base = np.mean(diffs)
    return float(left_seam), float(right_seam), float(base)


def dup_corr(mode, side):
    a = np.asarray(imgs[mode]).astype(np.float32)
    center = a[:, 448:1120, :]  # 672x672
    # resize center to 448x672
    center_img = Image.fromarray(center.astype(np.uint8))
    resized = center_img.resize((448, 672), Image.LANCZOS)
    r = np.asarray(resized).astype(np.float32)
    if side == "left":
        region = a[:, 0:448, :]
    else:
        region = a[:, 1120:1568, :]
    # pearson per channel averaged
    corrs = []
    for c in range(3):
        x = r[:, :, c].ravel()
        y = region[:, :, c].ravel()
        xm = x - x.mean()
        ym = y - y.mean()
        denom = np.sqrt((xm * xm).sum() * (ym * ym).sum())
        corrs.append(float((xm * ym).sum() / denom) if denom > 0 else 0.0)
    return float(np.mean(corrs))


def region_mad(mode, x0, x1):
    a = np.asarray(imgs[mode]).astype(np.float32)
    diff = np.abs(a[:, x0:x1, :] - work_arr[:, x0:x1, :])
    return float(diff.mean())


metrics = {}
for mode, rid in conds:
    ls, rs, base = seam_means(mode)
    left_corr = dup_corr(mode, "left")
    right_corr = dup_corr(mode, "right")
    metrics[mode] = {
        "run_id": rid,
        "capture_dir": str(CAP_BASE / rid),
        "final_path": str(CAP_BASE / rid / "final.png"),
        "seam_left_mean": ls,
        "seam_right_mean": rs,
        "baseline_mean": base,
        "seam_ratio_left": (ls / base) if base > 0 else None,
        "seam_ratio_right": (rs / base) if base > 0 else None,
        "dup_corr_left": left_corr,
        "dup_corr_right": right_corr,
        "mad_left_margin": region_mad(mode, 0, 448),
        "mad_right_margin": region_mad(mode, 1120, 1568),
        "mad_center": region_mad(mode, 448, 1120),
    }

with open("/tmp/opencode/outpaint-ab/metrics.json", "w") as f:
    json.dump(metrics, f, indent=2, ensure_ascii=False)

# --- contact sheet ---
labeled = []
for mode, rid in conds:
    im = imgs[mode].copy()
    labeled.append((mode, im))

# create canvas
total_h = CH * len(labeled)
sheet = Image.new("RGB", (CW, total_h), (0, 0, 0))
yoff = 0
try:
    from PIL import ImageDraw, ImageFont

    _font = ImageFont.load_default()
    _have_font = True
except Exception:
    _have_font = False
for mode, im in labeled:
    sheet.paste(im, (0, yoff))
    if _have_font:
        d = ImageDraw.Draw(sheet)
        d.text((10, yoff + 10), mode.upper(), fill=(255, 255, 0), font=_font)
    yoff += CH
sheet.save("/tmp/opencode/outpaint-ab/contact.png")
print("CONTACT saved", flush=True)
print(json.dumps(metrics, indent=2, ensure_ascii=False))
