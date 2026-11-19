"""Assertion de conservation des pixels protégés après composite."""

from __future__ import annotations

import io
from PIL import Image

try:  # numpy optionnel mais utilisé pour la performance.
    import numpy as np
except Exception:  # pragma: no cover - numpy absent
    np = None  # type: ignore[assignment]


def assert_protected_preserved(
    final_png: bytes, work_png: bytes, mask_png: bytes, *, threshold: int = 128
) -> None:
    """Lève RuntimeError si un pixel protégé (mask < threshold) diffère final/work.

    La comparaison porte sur les 4 canaux RGBA. Le masque est converti en L.
    """
    try:
        with Image.open(io.BytesIO(final_png)) as f:
            f.load()
            final = f.convert("RGBA")
        with Image.open(io.BytesIO(work_png)) as w:
            w.load()
            work = w.convert("RGBA")
        with Image.open(io.BytesIO(mask_png)) as m:
            m.load()
            mask = m.convert("L")
    except Exception as exc:
        raise ValueError(f"[BAD_ARGS] PNG illisible : {exc}") from exc

    if final.size != work.size or final.size != mask.size:
        raise ValueError("[DIMENSIONS_MISMATCH] Dimensions incompatibles.")

    width, height = final.size

    if np is not None:
        final_arr = np.asarray(final, dtype=np.uint8).reshape(height, width, 4)
        work_arr = np.asarray(work, dtype=np.uint8).reshape(height, width, 4)
        mask_arr = np.asarray(mask, dtype=np.uint8).reshape(height, width)
        protected = mask_arr < threshold
        diff = np.any(final_arr != work_arr, axis=-1)
        count = int(np.count_nonzero(protected & diff))
    else:
        fpx = final.load()
        wpx = work.load()
        mpx = mask.load()
        count = 0
        for y in range(height):
            for x in range(width):
                if int(mpx[x, y]) < threshold:
                    if fpx[x, y] != wpx[x, y]:
                        count += 1

    if count > 0:
        raise RuntimeError(f"[COMPOSITE_LEAK] {count} pixels protégés modifiés")
