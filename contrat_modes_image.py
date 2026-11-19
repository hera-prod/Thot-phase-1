"""Contrat pédagogique des SIX modes de Thot, sans chargement de modèle.

Noms proposés pour un contrat interne : les adapter aux APIs existantes,
avec mapping explicite et tests. Ne constitue pas l'implémentation des
pipelines ni une preuve de capacité réelle.
"""
from dataclasses import dataclass
from enum import Enum


class Mode(str, Enum):
    TEXT_TO_IMAGE = "text_to_image"
    IMAGE_EDIT = "image_edit"
    MULTI_IMAGE_EDIT = "multi_image_edit"
    INPAINT = "inpaint"
    OUTPAINT = "outpaint"
    REGION_EDIT = "region_edit"


class ContractError(ValueError):
    pass


@dataclass(frozen=True)
class Request:
    mode: Mode
    prompt: str
    width: int
    height: int
    source_id: str | None = None
    reference_ids: tuple[str, ...] = ()
    mask_id: str | None = None
    mask_kind: str | None = None


def validate(req: Request) -> None:
    if not isinstance(req.mode, Mode):
        raise ContractError("Mode explicite requis ; aucun fallback implicite.")
    if not isinstance(req.prompt, str) or not req.prompt.strip():
        raise ContractError("Instruction non vide requise.")
    if any(type(x) is not int or x <= 0 for x in (req.width, req.height)):
        raise ContractError("Dimensions positives requises ; la grille du moteur est à valider séparément.")
    if req.source_id is not None and (not isinstance(req.source_id, str) or not req.source_id.strip()):
        raise ContractError("Référence source invalide.")
    if not isinstance(req.reference_ids, tuple) or any(not isinstance(x, str) or not x.strip() for x in req.reference_ids):
        raise ContractError("Références ordonnées invalides.")
    if req.mask_id is not None and (not isinstance(req.mask_id, str) or not req.mask_id.strip()):
        raise ContractError("Référence masque invalide.")
    total = (req.source_id is not None) + len(req.reference_ids)
    if total > 4:
        raise ContractError("Quatre images au maximum, source principale comprise.")

    if req.mode is Mode.TEXT_TO_IMAGE:
        if total or req.mask_id is not None or req.mask_kind is not None:
            raise ContractError("T2I : aucune source, référence ou masque.")
    elif req.mode is Mode.IMAGE_EDIT:
        if req.source_id is None or req.reference_ids or req.mask_id is not None or req.mask_kind is not None:
            raise ContractError("Édition simple : une source, aucun masque ni référence supplémentaire.")
    elif req.mode is Mode.MULTI_IMAGE_EDIT:
        if req.source_id is None or not (2 <= total <= 4) or req.mask_id is not None or req.mask_kind is not None:
            raise ContractError("Édition multi : source + 1 à 3 références, sans masque.")
    else:
        expected = "edit" if req.mode is Mode.REGION_EDIT else "fill"
        if req.source_id is None or req.reference_ids or req.mask_id is None or req.mask_kind != expected:
            raise ContractError(f"Mode masqué : une source, masque {expected}, sans référence additionnelle dans ce lot.")


def ordered_edit_inputs(req: Request) -> tuple[str, ...]:
    validate(req)
    if req.mode not in (Mode.IMAGE_EDIT, Mode.MULTI_IMAGE_EDIT):
        raise ContractError("Cette liste concerne l'édition par références, pas les modes masqués.")
    return (req.source_id,) + req.reference_ids


def route_family(req: Request) -> str:
    validate(req)
    return {
        Mode.TEXT_TO_IMAGE: "text_generation",
        Mode.IMAGE_EDIT: "reference_edit",
        Mode.MULTI_IMAGE_EDIT: "reference_edit",
        Mode.INPAINT: "masked_fill",
        Mode.OUTPAINT: "masked_fill",
        Mode.REGION_EDIT: "masked_edit",
    }[req.mode]


def compose_protected(source, candidate, mask):
    """Recomposition L binaire, blanc = remplacer, noir = protéger.

    Ce contrôle NE prouve PAS la qualité du contenu généré ni la fidélité
    d'une édition à l'intérieur du masque. Aucun appel de modèle.
    """
    from PIL import Image
    if source.mode != "RGB" or candidate.mode != "RGB" or mask.mode != "L":
        raise ContractError("Source/candidat RGB et masque L explicites requis.")
    if source.size != candidate.size or source.size != mask.size:
        raise ContractError("Mêmes dimensions et même géométrie requises.")
    values = {i for i, count in enumerate(mask.histogram()) if count}
    if not values.issubset({0, 255}):
        raise ContractError("Traiter explicitement les bords doux ; masque binaire attendu par ce contrôle.")
    return Image.composite(candidate, source, mask)
