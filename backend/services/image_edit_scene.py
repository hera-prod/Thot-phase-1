"""Scène d'édition image : work + masque, sans GPU.

Ce module construit exactement la toile que le studio d'édition envoie au
moteur : la source est ajustée en conservant son ratio (`fit`) et centrée ;
les marges sont comblées par prolongement des bords (clamp-to-edge), jamais
par un gris ni un flou. L'outpainting ajoute un masque `L` blanc hors du
rectangle source et noir dedans ; l'édition globale n'a pas de masque.

Aucun décodage de pixels n'est fait ailleurs : les octets d'artefact entrent
par `build_global_scene` / `build_outpaint_scene`, les PNG de staging en
sortent, déjà bornés et signés.
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageDraw, ImageOps

from backend.services.image_service import FORMATS, _IMAGE_STUDIO_MAX_BYTES

_MAX_SOURCE_PIXELS = 40_000_000
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MIN_OUTPAINT_MARGIN_PX = 16


@dataclass(frozen=True)
class EditScene:
    work_png: bytes
    mask_png: bytes | None
    canvas_width: int
    canvas_height: int
    aspect_ratio: str
    source_rect: dict[str, int]
    source_size: dict[str, int]


def _validate_ratio(aspect_ratio: str) -> str:
    ratio = (aspect_ratio or "").strip() if isinstance(aspect_ratio, str) else ""
    if ratio not in FORMATS:
        raise ValueError(f"[ENUM_INVALID] aspect_ratio parmi {', '.join(FORMATS)}.")
    return ratio


def nearest_format(width: int, height: int) -> str:
    """Format officiel dont le logarithme du ratio est le plus proche.

    Le logarithme compare des rapports sans qu'un format large écrase un
    format haut. À distance égale, l'ordre de `FORMATS` départage : le premier
    déclaré gagne, donc le résultat est déterministe.
    """
    if not isinstance(width, int) or not isinstance(height, int):
        raise ValueError("[BAD_ARGS] Dimensions source invalides.")
    if width <= 0 or height <= 0:
        raise ValueError("[BAD_ARGS] Dimensions source invalides.")

    source_log_ratio = math.log(width / height)
    best_ratio = next(iter(FORMATS))
    best_distance = math.inf
    for ratio, (format_width, format_height) in FORMATS.items():
        distance = abs(math.log(format_width / format_height) - source_log_ratio)
        if distance < best_distance:
            best_ratio = ratio
            best_distance = distance
    return best_ratio


def source_format(source_bytes: bytes) -> str:
    """Format officiel le plus proche de la source, sans construire de scène.

    Sert à détecter une demande de changement de format exprimée comme un
    `edit` : le décodage (EXIF, alpha, limites) reste celui du studio, donc le
    format lu est exactement celui que la scène aurait utilisé.
    """
    source = _decode_source(source_bytes)
    try:
        return nearest_format(source.width, source.height)
    finally:
        source.close()


def _decode_source(source_bytes: bytes) -> Image.Image:
    try:
        with Image.open(io.BytesIO(source_bytes)) as source:
            width, height = source.size
            if width * height > _MAX_SOURCE_PIXELS:
                raise ValueError("[TOO_LARGE] Image source trop grande.")
            transposed = ImageOps.exif_transpose(source) or source
            try:
                transposed.load()
                if "A" in transposed.getbands() or (
                    transposed.mode == "P" and "transparency" in transposed.info
                ):
                    rgba = (
                        transposed
                        if transposed.mode == "RGBA"
                        else transposed.convert("RGBA")
                    )
                    work = Image.new("RGB", rgba.size, (128, 128, 128))
                    work.paste(rgba, mask=rgba.getchannel("A"))
                else:
                    work = transposed.convert("RGB")
            finally:
                if transposed is not source:
                    transposed.close()
        if work.width * work.height > _MAX_SOURCE_PIXELS:
            raise ValueError("[TOO_LARGE] Image source trop grande.")
        return work
    except ValueError as exc:
        if str(exc).startswith("[TOO_LARGE]"):
            raise
        raise ValueError("[PNG_INVALID] Image source illisible.") from exc
    except Exception as exc:
        raise ValueError("[PNG_INVALID] Image source illisible.") from exc


def _fit_rect(
    source_width: int, source_height: int, canvas_width: int, canvas_height: int
) -> tuple[int, int, int, int]:
    """Rectangle centré de la source ajustée dans la toile, sans déformation."""
    scale = min(canvas_width / source_width, canvas_height / source_height)
    width = min(canvas_width, max(1, round(source_width * scale)))
    height = min(canvas_height, max(1, round(source_height * scale)))
    x = (canvas_width - width) // 2
    y = (canvas_height - height) // 2
    return x, y, width, height


def _clamp_to_edge(
    source: Image.Image, rect: tuple[int, int, int, int], canvas_size: tuple[int, int]
) -> Image.Image:
    """Prolonge la source ajustée sur toute la toile, bord le plus proche.

    L'indexation `clip` est exactement le résultat des deux passes décrites :
    marges horizontales sur toute la hauteur (colonne 0 / dernière colonne),
    puis marges verticales sur toute la largeur (ligne 0 / dernière ligne).
    Les coins prennent ainsi le pixel de coin, cohérent avec les deux passes.
    """
    x, y, width, height = rect
    canvas_width, canvas_height = canvas_size
    resized = source.resize((width, height), Image.Resampling.LANCZOS)
    pixels = np.asarray(resized, dtype=np.uint8)
    rows = np.clip(np.arange(canvas_height) - y, 0, height - 1)
    columns = np.clip(np.arange(canvas_width) - x, 0, width - 1)
    return Image.fromarray(pixels[np.ix_(rows, columns)], "RGB")


def _outpaint_mask(
    rect: tuple[int, int, int, int], canvas_size: tuple[int, int]
) -> Image.Image:
    x, y, width, height = rect
    canvas_width, canvas_height = canvas_size
    mask = Image.new("L", (canvas_width, canvas_height), 255)
    ImageDraw.Draw(mask).rectangle([x, y, x + width - 1, y + height - 1], fill=0)
    return mask


def _encode_png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    data = buffer.getvalue()
    if not data.startswith(_PNG_SIGNATURE):
        raise ValueError("[PNG_INVALID] Encodage PNG invalide.")
    if len(data) > _IMAGE_STUDIO_MAX_BYTES:
        raise ValueError("[PNG_TOO_LARGE] PNG de scène trop volumineux.")
    return data


def _build_scene(
    source: Image.Image,
    aspect_ratio: str,
    *,
    with_mask: bool,
) -> EditScene:
    source_size = {"width": source.width, "height": source.height}
    canvas_width, canvas_height = FORMATS[aspect_ratio]
    rect = _fit_rect(source.width, source.height, canvas_width, canvas_height)
    source_rect = {
        "x": rect[0],
        "y": rect[1],
        "width": rect[2],
        "height": rect[3],
    }
    work = _clamp_to_edge(source, rect, (canvas_width, canvas_height))
    mask = _outpaint_mask(rect, (canvas_width, canvas_height)) if with_mask else None
    return EditScene(
        work_png=_encode_png(work),
        mask_png=_encode_png(mask) if mask is not None else None,
        canvas_width=canvas_width,
        canvas_height=canvas_height,
        aspect_ratio=aspect_ratio,
        source_rect=source_rect,
        source_size=source_size,
    )


def build_global_scene(
    source_bytes: bytes, aspect_ratio: str | None = None
) -> EditScene:
    source = _decode_source(source_bytes)
    try:
        ratio = (
            nearest_format(source.width, source.height)
            if aspect_ratio is None
            else _validate_ratio(aspect_ratio)
        )
        return _build_scene(source, ratio, with_mask=False)
    finally:
        source.close()


def build_outpaint_scene(source_bytes: bytes, aspect_ratio: str) -> EditScene:
    ratio = _validate_ratio(aspect_ratio)
    source = _decode_source(source_bytes)
    try:
        canvas_width, canvas_height = FORMATS[ratio]
        _x, _y, width, height = _fit_rect(
            source.width, source.height, canvas_width, canvas_height
        )
        horizontal_margin = canvas_width - width
        vertical_margin = canvas_height - height
        if (
            horizontal_margin < MIN_OUTPAINT_MARGIN_PX
            and vertical_margin < MIN_OUTPAINT_MARGIN_PX
        ):
            raise ValueError(
                "[BAD_ARGS] [OUTPAINT_NO_MARGIN] L’image a déjà ce format ; "
                "utilisez l’édition pour la retoucher."
            )
        return _build_scene(source, ratio, with_mask=True)
    finally:
        source.close()


__all__ = [
    "EditScene",
    "MIN_OUTPAINT_MARGIN_PX",
    "build_global_scene",
    "build_outpaint_scene",
    "nearest_format",
    "source_format",
]
