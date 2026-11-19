"""Diagnostic O1 réel : source paysage 1280x720 dans canevas portrait 720x1280."""

import hashlib
import io
import json
import sys
from pathlib import Path

from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.check_captures_v4 import load_produced_manifest, prepare_capture_producer

RUN_ID = "v4-o1-noref-src"
DATA, cap, inputs = prepare_capture_producer("v4-captures-o1", RUN_ID)

import backend.services.image_service as svc
from backend.config.paths import get_data_dir as _gdd
from backend.db.models import Base
from backend.services.capture_manifest import manifest_valid

engine = create_engine(f"sqlite:///{DATA / 'diag.db'}")
Base.metadata.create_all(bind=engine)
Session = sessionmaker(
    bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
)

svc.check_install = lambda: (True, "ok")
svc.check_vram = lambda: (True, "ok")
svc._rendre_la_vram_du_worker_ocr = lambda: "ok"

src_asset = Image.open(ROOT / "scripts/assets/cafe-1.png").convert("RGB")
source = src_asset.resize((1280, 720), Image.LANCZOS)
source.save(inputs / "source.png")

# canevas portrait 720x1280
CW, CH = 720, 1280
SW, SH = 1280, 720
# fitRectToCanvas for canvas CWxCH, source SWxSH
scale = min(CW / SW, CH / SH)
rw = (
    SW * scale
)  # 720? 720*? scale min(720/1280=0.5625,1280/720=1.777)=0.5625 => rw=405? wait
# recalc explicitly expected
scale = min(CW / SW, CH / SH)
rw = max(1, SW * scale)
rh = max(1, SH * scale)
rx = (CW - rw) / 2
ry = (CH - rh) / 2
# Ce calcul donne x=0, y=437.5, w=720, h=405 (fitRectToCanvas, canevas 720x1280).

transform = {"x": rx, "y": ry, "width": rw, "height": rh}
source_size = {"width": SW, "height": SH}

work = Image.new("RGB", (CW, CH), (0, 0, 0))
small = source.resize((round(rw), round(rh)), Image.LANCZOS)
work.paste(small, (round(rx), round(ry)))
px = work.load()
for y in range(CH):
    for x in range(CW):
        inside = x >= rx and x < rx + rw and y >= ry and y < ry + rh
        if inside:
            continue
        sx = min(max(x, round(rx)), round(rx) + round(rw) - 1)
        sy = min(max(y, round(ry)), round(ry) + round(rh) - 1)
        px[x, y] = px[sx, sy]
work.save(inputs / "work.png")

mask = Image.new("L", (CW, CH), 0)
mp = mask.load()
for y in range(CH):
    for x in range(CW):
        inside = x >= rx and x < rx + rw and y >= ry and y < ry + rh
        mp[x, y] = 0 if inside else 255
mask.save(inputs / "mask.png")

studio = _gdd() / "temp" / "image-studio"
studio.mkdir(parents=True, exist_ok=True)


def stage(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    data = buf.getvalue()
    tok = hashlib.md5(data).hexdigest()
    (studio / f"{tok}.png").write_bytes(data)
    return tok, hashlib.sha256(data).hexdigest()


work_tok, work_sha = stage(work)
mask_tok, mask_sha = stage(mask)

prompt = (
    "Reconstruct and extend naturally the masked area matching the surrounding scene."
)

res = svc.generate(
    None,
    RUN_ID,
    prompt,
    "9:16",
    [],
    1234,
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
    json.dumps({k: res.get(k) for k in ("artifact_id", "sha256")}, ensure_ascii=False),
)

man = load_produced_manifest(cap)

print("MANIFEST_VALID", manifest_valid(man, cap))
print(
    "RUN_TYPE",
    man.get("run_type"),
    "STATUS",
    man.get("capture_status"),
    "STEPS",
    man.get("inference", {}).get("steps_executed"),
)
print("MASK_METRICS", json.dumps(man.get("mask_metrics"), ensure_ascii=False)[:2000])

source.save(cap / "source.png")


def load(name):
    return Image.open(cap / name).convert("RGB")


cand, final, wk = (
    load("candidate_before_composite.png"),
    load("final.png"),
    load("work.png"),
)
msk = Image.open(cap / "mask_backend.png").convert("L")


def band_stats(a, b, m, y0, y1):
    n = changed = 0
    tot = 0
    for y in range(y0, y1):
        for x in range(a.size[0]):
            if m.getpixel((x, y)) < 128:
                continue
            n += 1
            pa, pb = a.getpixel((x, y)), b.getpixel((x, y))
            d = sum(abs(pa[i] - pb[i]) for i in range(3))
            tot += d
            if d > 8:
                changed += 1
    return {
        "pixels": n,
        "changed_gt8": changed,
        "changed_ratio": round(changed / max(1, n), 4),
        "mean_abs_diff": round(tot / max(1, n) / 3, 2),
    }


top_bound = 438  # y < 437.5
bottom_bound = 843  # y >= 842.5
print("TOP_MARGIN", band_stats(cand, wk, msk, 0, top_bound))
print("BOTTOM_MARGIN", band_stats(cand, wk, msk, bottom_bound, CH))
leak = 0
for y in range(top_bound, bottom_bound):
    for x in range(CW):
        if msk.getpixel((x, y)) < 128 and final.getpixel((x, y)) != wk.getpixel((x, y)):
            leak += 1
print("CENTER_LEAK", leak)
