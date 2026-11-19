"""Diagnostic I1 réel : édition intérieure sur source 512x512."""

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

RUN_ID = "v4-i1-noref-src"
DATA, cap, inputs = prepare_capture_producer("v4-captures-i1", RUN_ID)

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

# Mutation temporaire du registre de formats du service (uniquement ce diagnostic).
# Elle est restaurée quoi qu'il arrive, même en cas d'échec, pour ne pas
# polluer le service pour les appels suivants.
_saved_formats = svc.FORMATS
svc.FORMATS = dict(svc.FORMATS)
svc.FORMATS["_diag_512"] = (512, 512)


def main() -> None:
    src_asset = Image.open(ROOT / "scripts/assets/personne-1.png").convert("RGB")
    source = src_asset.resize((512, 512), Image.LANCZOS)
    source.save(inputs / "source.png")

    CW, CH = 512, 512
    SW, SH = 512, 512
    x0, x1 = 176, 336
    y0, y1 = 96, 256
    transform = {"x": 0.0, "y": 0.0, "width": 512.0, "height": 512.0}
    source_size = {"width": SW, "height": SH}

    work = source.copy()
    mask = Image.new("L", (CW, CH), 0)
    mp = mask.load()
    for y in range(CH):
        for x in range(CW):
            if x0 <= x < x1 and y0 <= y < y1:
                mp[x, y] = 255
            else:
                mp[x, y] = 0
    work.save(inputs / "work.png")
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

    prompt = "Replace the selected region with a polished chrome robot mask, keeping the same lighting and pose."

    res = svc.generate(
        None,
        RUN_ID,
        prompt,
        "1:1",
        [],
        1234,
        db_session_factory=Session,
        origin="studio",
        mode="local",
        work_token=work_tok,
        mask_token=mask_tok,
        work_sha256=work_sha,
        mask_sha256=mask_sha,
        canvas_width=CW,
        canvas_height=CH,
        strength=0.55,
        mask_kind="edit",
        source_transform=transform,
        source_size=source_size,
    )
    print(
        "RESULT",
        json.dumps(
            {k: res.get(k) for k in ("artifact_id", "sha256")}, ensure_ascii=False
        ),
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

    source.save(cap / "source.png")

    def load(name):
        return Image.open(cap / name).convert("RGB")

    cand, final = load("candidate_before_composite.png"), load("final.png")
    src_img = source

    # allowed zone stats
    n = changed = 0
    tot = 0
    for y in range(y0, y1):
        for x in range(x0, x1):
            n += 1
            pc = cand.getpixel((x, y))
            ps = src_img.getpixel((x, y))
            d = sum(abs(pc[i] - ps[i]) for i in range(3))
            tot += d
            if d > 8:
                changed += 1
    print("ALLOWED_CHANGED_RATIO", round(changed / max(1, n), 4))
    print("ALLOWED_MEAN_ABS_DIFF", round(tot / max(1, n) / 3, 2))

    # protected outside max delta (final vs source)
    maxd = 0
    for y in range(CH):
        for x in range(CW):
            if x0 <= x < x1 and y0 <= y < y1:
                continue
            pf = final.getpixel((x, y))
            ps = src_img.getpixel((x, y))
            d = max(abs(pf[i] - ps[i]) for i in range(3))
            maxd = max(maxd, d)
    print("PROTECTED_MAX_DELTA", maxd)


if __name__ == "__main__":
    try:
        main()
    finally:
        svc.FORMATS = _saved_formats
