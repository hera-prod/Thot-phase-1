"""Métriques spatiales pour un masque PNG (backend image, V4).

Fonction pure : entrée des octets PNG, retour d'un dict structuré. Aucun effet
de bord, aucune dépendance au réseau.
"""

from __future__ import annotations

import io
from typing import Any

from PIL import Image


def _bbox(points: list[tuple[int, int]]) -> dict[str, int] | None:
    if not points:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return {"x0": min(xs), "y0": min(ys), "x1": max(xs), "y1": max(ys)}


def mask_metrics(mask_png: bytes, *, transform: dict | None = None) -> dict:
    """Calcule les métriques d'un masque PNG (octets).

    Pixels autorisés (éditables) = valeur ``>= 128``. Les régions sont disjointes.

    Ordre de priorité des régions : ``top``/``bottom`` avant ``left``/``right``
    avant ``center``. Un pixel frontalier appartient à une seule région
    (la première applicable dans cet ordre), ce qui garantit la disjointure et
    une couverture exacte de ``width × height``.

    Avec ``transform``, les régions dérivent du rectangle source fourni ; sans,
    ce sont les bandes d'un pixel au bord + le centre.
    """
    try:
        with Image.open(io.BytesIO(mask_png)) as img:
            img.load()
            mask = img.convert("L")
    except Exception as exc:  # pragma: no cover - chemin d'erreur
        raise ValueError(f"[MASK_EMPTY] [BAD_ARGS] Masque illisible : {exc}") from exc

    # Image.open peut ne pas lever tant que l'on ne lit rien ; on force la décode.
    try:
        mask.load()
    except Exception as exc:
        raise ValueError(f"[MASK_EMPTY] [BAD_ARGS] Masque illisible : {exc}") from exc

    width, height = mask.size

    # Compteurs par ligne / colonne des pixels autorisés.
    rows: list[int] = [0] * height
    cols: list[int] = [0] * width
    editable_points: list[tuple[int, int]] = []
    protected_points: list[tuple[int, int]] = []

    # Pré-calcul de la valeur de chaque pixel pour éviter double lecture.
    values: list[list[int]] = [[0] * width for _ in range(height)]
    for y in range(height):
        for x in range(width):
            v = int(mask.getpixel((x, y)))  # type: ignore[arg-type]
            values[y][x] = v
            if v >= 128:
                rows[y] += 1
                cols[x] += 1
                editable_points.append((x, y))
            else:
                protected_points.append((x, y))

    total_editable = len(editable_points)

    # Classification d'un pixel en région (priorité top/bottom puis left/right
    # puis center). Retourne le nom de la région.
    def region_of(x: int, y: int) -> str:
        if transform is not None:
            tx = transform["x"]
            ty = transform["y"]
            tw = transform["width"]
            th = transform["height"]
            if y < ty:
                return "top"
            if y >= ty + th:
                return "bottom"
            if x < tx:
                return "left"
            if x >= tx + tw:
                return "right"
            return "center"
        # Sans transform : bandes de 1 px.
        if y == 0:
            return "top"
        if y == height - 1:
            return "bottom"
        if x == 0:
            return "left"
        if x == width - 1:
            return "right"
        return "center"

    # Initialisation des régions.
    region_names = ["top", "bottom", "left", "right", "center"]
    region_pixels: dict[str, int] = {r: 0 for r in region_names}
    region_editable: dict[str, int] = {r: 0 for r in region_names}
    region_editable_points: dict[str, list[tuple[int, int]]] = {
        r: [] for r in region_names
    }
    region_protected_points: dict[str, list[tuple[int, int]]] = {
        r: [] for r in region_names
    }

    for y in range(height):
        for x in range(width):
            r = region_of(x, y)
            region_pixels[r] += 1
            if values[y][x] >= 128:
                region_editable[r] += 1
                region_editable_points[r].append((x, y))
            else:
                region_protected_points[r].append((x, y))

    coverage_by_region: dict[str, dict[str, Any]] = {}
    for r in region_names:
        pixels = region_pixels[r]
        editable = region_editable[r]
        ratio = (editable / pixels) if pixels != 0 else 0.0
        coverage_by_region[r] = {
            "pixels": pixels,
            "editable": editable,
            "ratio": ratio,
            "retained_bbox": _bbox(region_editable_points[r]),
            "lost_bbox": _bbox(region_protected_points[r]),
        }

    transform_source = "provided" if transform is not None else "absent"

    return {
        "width": width,
        "height": height,
        "total_editable": total_editable,
        "rows": rows,
        "cols": cols,
        "bbox": _bbox(editable_points),
        "coverage_by_region": coverage_by_region,
        "transform_source": transform_source,
    }
