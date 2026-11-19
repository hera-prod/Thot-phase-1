"""Diagnostic V4 : T2I pur + multi-référence (non-régression sampler V4).

Deux runs réels de non-régression après le correctif sampler V4 :
- T2I pur (mode None, aucun masque, aucune init) ;
- multi-référence (deux images de référence, aucune init, aucun masque).

Ces deux chemins ne doivent pas être affectés par le correctif `_cond_ref`
(qui ne s'active qu'en mode masqué). Preuve d'exécution réelle uniquement.
"""

import hashlib, io, json, os, pathlib, shutil, sys, traceback

DATA = pathlib.Path("/tmp/opencode/v4-t2i-data")
DATA.mkdir(parents=True, exist_ok=True)
os.environ["THOT_DATA_DIR"] = str(DATA)
os.environ["THOT_IMAGE_CAPTURE_DIR"] = "/tmp/opencode/v4-captures-t2i"
os.environ["THOT_IMAGE_MASK_DIAGNOSTICS"] = "1"
CAP = pathlib.Path(os.environ["THOT_IMAGE_CAPTURE_DIR"])
CAP.mkdir(parents=True, exist_ok=True)
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
from backend.config.paths import get_data_dir as _gdd
from backend.services.capture_manifest import manifest_valid

engine = create_engine(f"sqlite:///{DATA / 'diag.db'}")
Base.metadata.create_all(bind=engine)
Session = sessionmaker(
    bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
)

svc.check_install = lambda: (True, "ok")
svc.check_vram = lambda: (True, "ok")
svc._rendre_la_vram_du_worker_ocr = lambda: "ok"


def stage(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    data = buf.getvalue()
    tok = hashlib.md5(data).hexdigest()
    studio = _gdd() / "temp" / "image-studio"
    studio.mkdir(parents=True, exist_ok=True)
    (studio / f"{tok}.png").write_bytes(data)
    return tok, hashlib.sha256(data).hexdigest()


def image_stats(path):
    im = Image.open(path).convert("RGB")
    colors = len(set(im.getdata()))
    arr = np.asarray(im, dtype=np.float64)
    std = float(arr.std())
    return colors, std, im.size


def verify_constant(path, label):
    try:
        colors, std, size = image_stats(path)
        ok = colors > 1000 and std > 1.0
        print(f"{label}_UNIQUE_COLORS {colors}")
        print(f"{label}_STD {std:.6f}")
        print(f"{label}_CONSTANT_CHECK {'PASS' if ok else 'FAIL'}")
        return ok, colors, std, size
    except Exception as exc:
        print(f"{label}_STATS_ERROR {exc!r}")
        return False, None, None, None


def run_labeled(run_id, fn):
    print(f"=== RUN {run_id} ===")
    shutil.rmtree(CAP / run_id, ignore_errors=True)
    try:
        res = fn()
    except Exception as exc:
        print(f"RUN_ERROR {run_id} {exc!r}")
        traceback.print_exc()
        return None
    print(
        "RESULT",
        json.dumps(
            {k: res.get(k) for k in ("artifact_id", "sha256")}, ensure_ascii=False
        ),
    )
    cap = CAP / run_id
    man = json.loads((cap / "manifest.json").read_text())
    mv = manifest_valid(man, cap)
    print("MANIFEST_VALID", mv)
    print(
        "RUN_TYPE",
        man.get("run_type"),
        "STATUS",
        man.get("capture_status"),
        "STEPS",
        man.get("inference", {}).get("steps_executed"),
    )
    print("SIZE", f"{man.get('width')}x{man.get('height')}")
    print("REFERENCES_COUNT", man.get("references", {}).get("count"))
    print("SOURCE_SHA256", man.get("source", {}).get("source_sha256"))
    print("MODE", man.get("mode"))
    print("MASK_METRICS", repr(man.get("mask_metrics")))
    print("MASK_METRICS_KEY_ABSENT", "mask_metrics" not in man)

    # existence checks
    cand_path = cap / "candidate_before_composite.png"
    final_path = cap / "final.png"
    work_path = cap / "work.png"
    mask_path = cap / "mask_backend.png"
    print("CANDIDATE_EXISTS", cand_path.is_file())
    print("FINAL_EXISTS", final_path.is_file())
    print("HAS_WORK_PNG", work_path.is_file())
    print("HAS_MASK_BACKEND_PNG", mask_path.is_file())

    cand_ok = cand_path.is_file()
    final_ok = final_path.is_file()

    print("--- candidate_before_composite.png ---")
    cand_const_ok, _, _, _ = (
        verify_constant(cand_path, "CANDIDATE")
        if cand_ok
        else (False, None, None, None)
    )
    print("--- final.png ---")
    final_const_ok, _, _, _ = (
        verify_constant(final_path, "FINAL") if final_ok else (False, None, None, None)
    )
    if cand_ok:
        print("CANDIDATE_CONST_CHECK", "PASS" if cand_const_ok else "FAIL")
    if final_ok:
        print("FINAL_CONST_CHECK", "PASS" if final_const_ok else "FAIL")

    # Required compact UNIQUE_COLORS / STD (final artifact)
    if final_ok:
        colors, std, size = image_stats(final_path)
        print("UNIQUE_COLORS", colors)
        print("STD", std)
    return {
        "run_id": run_id,
        "manifest_valid": mv[0],
        "run_type": man.get("run_type"),
        "status": man.get("capture_status"),
        "steps": man.get("inference", {}).get("steps_executed"),
        "size": f"{man.get('width')}x{man.get('height')}",
        "final_unique_colors": colors if final_ok else None,
        "final_std": std if final_ok else None,
        "cap": str(cap),
        "has_work_png": work_path.is_file(),
        "has_mask_backend_png": mask_path.is_file(),
        "mask_metrics": man.get("mask_metrics"),
    }


def run_t2i():
    prompt = "A red ceramic teapot on a wooden table, soft window light."
    return svc.generate(
        None,
        "v4-t2i-real-v2",
        prompt,
        "1:1",
        [],
        1234,
        db_session_factory=Session,
        origin="studio",
        mode=None,
        strength=None,
        work_token=None,
        mask_token=None,
    )


def run_multiref():
    prompt = "Blend the two references: the subject from the second image in the setting of the first, same lighting."
    cafe = (
        Image.open("scripts/assets/cafe-1.png")
        .convert("RGB")
        .resize((1024, 1024), Image.LANCZOS)
    )
    person = (
        Image.open("scripts/assets/personne-1.png")
        .convert("RGB")
        .resize((1024, 1024), Image.LANCZOS)
    )
    tok_a, sha_a = stage(cafe)
    tok_b, sha_b = stage(person)
    return svc.generate(
        None,
        "v4-multiref-real-v2",
        prompt,
        "1:1",
        [],
        1234,
        db_session_factory=Session,
        origin="studio",
        mode="reference",
        reference_tokens=[tok_a, tok_b],
        reference_sha256=[sha_a, sha_b],
    )


summary_t2i = run_labeled("v4-t2i-real-v2", run_t2i)
summary_mr = run_labeled("v4-multiref-real-v2", run_multiref)

print("=== SUMMARY ===")
for s in (summary_t2i, summary_mr):
    if s is None:
        print("RUN FAILED (see above)")
        continue
    print(
        f"{s['run_id']} | manifest_valid={s['manifest_valid']} | run_type={s['run_type']} | "
        f"status={s['status']} | steps={s['steps']} | size={s['size']} | "
        f"unique_colors={s['final_unique_colors']} | std={s['final_std']} | cap={s['cap']}"
    )

# T2I-specific anomaly checks
if summary_t2i is not None:
    print("T2I_NO_WORK_PNG", not summary_t2i["has_work_png"])
    print("T2I_NO_MASK_BACKEND_PNG", not summary_t2i["has_mask_backend_png"])
    print("T2I_MASK_METRICS_ABSENT", summary_t2i["mask_metrics"] is None)
