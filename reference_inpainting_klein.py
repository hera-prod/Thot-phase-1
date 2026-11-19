"""Sonde d'interface Klein inpaint : aucun chargement ni téléchargement de poids.

Ce module n'implémente PAS Flux2KleinInpaintPipeline et ne rend pas un
pipeline KV compatible avec les masques. Il accepte une instance fournie
par l'intégration existante, vérifie son interface et recompose les pixels
protégés. La boucle d'inférence reste celle du pipeline vérifié.
"""
from inspect import signature
from PIL import Image


def run_inpaint_probe(
    pipe,
    source: Image.Image,
    repaint_mask: Image.Image,
    prompt: str,
    *,
    steps: int,
    generator,
    strength: float = 1.0,
    extra_kwargs: dict | None = None,
):
    """Masque L binaire : 255 = modifier, 0 = protéger.

    Source doit être le rendu de travail RGB déjà aligné au canevas et à la
    grille du moteur, sans damier ni overlay UI. Le choix du checkpoint,
    du generator, du dtype, des steps et des optimisations est externe.
    strength=1 est une valeur de sonde, PAS un optimum fournisseur garanti.
    """
    if source.mode != "RGB":
        raise ValueError("Fournir une source RGB dont la conversion a été contrôlée.")
    if repaint_mask.mode != "L":
        raise ValueError("Fournir un masque L explicite, pas un overlay RGBA.")
    if source.size != repaint_mask.size:
        raise ValueError("Source et masque doivent avoir les mêmes dimensions.")
    if not prompt.strip():
        raise ValueError("Prompt vide.")
    if not isinstance(steps, int) or steps < 1 or not (0 < strength <= 1):
        raise ValueError("steps >= 1 et 0 < strength <= 1 requis.")
    values = {value for value, count in enumerate(repaint_mask.histogram()) if count}
    if not values.issubset({0, 255}):
        raise ValueError("La sonde attend un masque binaire ; traiter les raccords séparément.")
    if "mask_image" not in signature(pipe.__call__).parameters:
        raise TypeError("Ce pipeline n'expose pas mask_image : ce n'est pas une preuve de support inpaint.")

    source_snapshot = source.copy()
    mask_snapshot = repaint_mask.copy()
    if mask_snapshot.getbbox() is None:
        return {"candidate": None, "final": source_snapshot, "no_op": True}

    kwargs = dict(extra_kwargs or {})
    reserved = {"prompt", "image", "mask_image", "width", "height", "strength",
                "num_inference_steps", "generator", "output_type"}
    if reserved.intersection(kwargs):
        raise ValueError("extra_kwargs ne doit pas écraser les entrées de la sonde.")
    if "prompt_embeds" in kwargs:
        raise ValueError("Pour tester des embeddings précalculés, adapter l'appel : pas prompt + prompt_embeds.")

    result = pipe(
        prompt=prompt,
        image=source_snapshot.copy(),
        mask_image=mask_snapshot.copy(),
        width=source_snapshot.width,
        height=source_snapshot.height,
        strength=strength,
        num_inference_steps=steps,
        generator=generator,
        output_type="pil",
        **kwargs,
    )
    candidate = result.images[0]
    if not isinstance(candidate, Image.Image):
        raise TypeError("Résultat non PIL : vérifier output_type et le runtime.")
    if candidate.size != source_snapshot.size:
        raise ValueError("Le moteur a modifié les dimensions : résoudre la grille, ne pas étirer en sortie.")
    candidate = candidate.convert("RGB")
    final = Image.composite(candidate, source_snapshot, mask_snapshot)
    return {"candidate": candidate, "final": final, "no_op": False}
