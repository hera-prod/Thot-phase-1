"""A/B référence source : avec/sans `_cond` source (flag THOT_IMAGE_NO_SOURCE_REFERENCE)."""

import hashlib, io, json, os, pathlib, sys

DATA = pathlib.Path("/tmp/opencode/v4-data")
DATA.mkdir(parents=True, exist_ok=True)
os.environ["THOT_DATA_DIR"] = str(DATA)
os.environ["THOT_IMAGE_CAPTURE_DIR"] = "/tmp/opencode/v4-captures"
os.environ["THOT_IMAGE_MASK_DIAGNOSTICS"] = "1"
CAP = pathlib.Path(os.environ["THOT_IMAGE_CAPTURE_DIR"])
CAP.mkdir(parents=True, exist_ok=True)
optin = DATA / "image_backend.optin"
optin.write_text("flux2\n")
os.environ["THOT_IMAGE_OPTIN_PATH"] = str(optin)

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from backend.db.models import Base
import backend.services.image_service as svc
from backend.config.paths import get_data_dir as _gdd

engine = create_engine(f"sqlite:///{DATA / 'diag_ab.db'}")
Base.metadata.create_all(bind=engine)
Session = sessionmaker(
    bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
)

svc.check_install = lambda: (True, "ok")
svc.check_vram = lambda: (True, "ok")
svc._rendre_la_vram_du_worker_ocr = lambda: "ok"
svc.FORMATS = dict(svc.FORMATS)
svc.FORMATS["_diag_512"] = (512, 512)

src_asset = Image.open("scripts/assets/cafe-1.png").convert("RGB")
source = src_asset.resize((512, 512), Image.LANCZOS)
source.save("/tmp/opencode/v4-ab-source.png")

CW, CH = 512, 512
SW, SH = 512, 512
transform = {"x": 0.0, "y": 0.0, "width": 512.0, "height": 512.0}
source_size = {"width": SW, "height": SH}

work = source.copy()  # init pleine image
mask = Image.new("L", (CW, CH), 0)
mp = mask.load()
for y in range(CH):
    for x in range(CW):
        if 128 <= x < 384 and 96 <= y < 320:
            mp[x, y] = 255
        else:
            mp[x, y] = 0
work.save("/tmp/opencode/v4-ab-work.png")
mask.save("/tmp/opencode/v4-ab-mask.png")

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

prompt = "Reconstruct the masked area naturally, consistent with the surrounding image."
hole = [(x, y) for y in range(96, 320) for x in range(128, 384)]


def run_one(run_id, no_ref):
    stage(work)
    stage(mask)
    if no_ref:
        os.environ["THOT_IMAGE_NO_SOURCE_REFERENCE"] = "1"
    else:
        os.environ.pop("THOT_IMAGE_NO_SOURCE_REFERENCE", None)
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
        run_id,
        json.dumps(
            {k: res.get(k) for k in ("artifact_id", "sha256")}, ensure_ascii=False
        ),
    )
    cap = CAP / run_id
    man = json.loads((cap / "manifest.json").read_text())
    from backend.services.capture_manifest import manifest_valid

    print(
        run_id,
        "MANIFEST_VALID",
        manifest_valid(man, cap),
        "STATUS",
        man.get("capture_status"),
    )
    return cap


cap_ref = run_one("ab-ref", False)
cap_noref = run_one("ab-noref", True)


def load(cap, name):
    return Image.open(cap / name).convert("RGB")


cand_ref = load(cap_ref, "candidate_before_composite.png")
cand_noref = load(cap_noref, "candidate_before_composite.png")
src_img = source


def mean_abs(a, b, pixels):
    tot = 0.0
    n = 0
    for x, y in pixels:
        pa = a.getpixel((x, y))
        pb = b.getpixel((x, y))
        tot += sum(abs(pa[i] - pb[i]) for i in range(3)) / 3.0
        n += 1
    return tot / max(1, n)


m_ref = mean_abs(cand_ref, src_img, hole)
m_noref = mean_abs(cand_noref, src_img, hole)
m_ab = mean_abs(cand_ref, cand_noref, hole)
print("MEAN_ABS_REF_VS_SOURCE", round(m_ref, 4))
print("MEAN_ABS_NOREF_VS_SOURCE", round(m_noref, 4))
print("MEAN_ABS_REF_VS_NOREF", round(m_ab, 4))
