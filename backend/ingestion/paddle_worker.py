#!/usr/bin/env python3
"""Worker persistant PaddleOCR-VL-1.6, utilisé pour l'OCR de pages et de crops.

POURQUOI UN WORKER SÉPARÉ : le modèle pèse 1,8 Go et son chargement prend
plusieurs secondes. Le garder résident évite de le recharger à chaque page d'un
PDF. Le worker parle JSONL sur stdin/stdout, une réponse par ligne, pour rester
détectable par `stdout-framer` côté Electron comme par tout autre lecteur.

POURQUOI LA VOIE NATIVE ET NON LE CODE DISTANT : le dépôt du modèle embarque un
`auto_map` qui pointe vers un `modeling_paddleocr_vl.py` écrit pour une fenêtre
de versions étroite de transformers (5.2 à 5.8). Sur la version installée, ce
code mort produit soit une récursion infinie, soit un masque d'attention
incompatible — j'ai perdu beaucoup de temps à le才发现. transformers embarque
une implémentation native de PaddleOCR-VL qui, elle, suit l'API courante.

D'où `trust_remote_code=False` : ce n'est pas une précaution, c'est
l'inverse d'une précaution. Sans ce drapeau on charge du code périmé ; avec,
on charge l'implémentation maintenue. Les avertissements `Unrecognized keys
in 'rope_parameters'` qui apparaissent au chargement sont un défaut connu de
validation côté transformers, sans effet sur le résultat.

Le prompt n'est jamais écrit à la main : `apply_chat_template` produit la
séquence exacte et surtout expandit le jeton image en autant de placeholders
que la grille de patchs en contient. Un prompt construit à la main produit un
modèle qui s'exécute et ne rend aucun texte lisible.

PROTOCOLE (JSONL sur stdin/stdout) :
  entrée    : {"id": 1, "op": "ocr"|"table"|"chart"|"formula"|"spotting"|"seal"|"layout",
               "image_b64": "<png>", "max_tokens": 8192}
  sortie    : {"id": 1, "ok": true, "text": "..."} ou {"id": 1, "ok": true, "regions": [...]
              ou {"id": 1, "ok": false, "error": "..."}
  démarrage : {"ready": true, "model": "PaddleOCR-VL-1.6"}
"""

from __future__ import annotations

import base64
import io
import json
import os
import sys
import threading

MODEL_NAME = "PaddleOCR-VL-1.6"
LAYOUT_MODEL_NAME = "PP-DocLayoutV3"

# Bornes du worker (Phase 7.2). Le worker reçoit des PNG encodés en base64 sur
# stdin : sans plafond, un `b64decode` ou une ligne géante allouait avant tout
# contrôle. 32 Mio couvre une page rendue à haute résolution sans autoriser un
# payload arbitraire ; la ligne JSON laisse la marge du base64 et de l'enveloppe.
try:
    from backend.config.defaults import INPUT_LIMITS as _INPUT_LIMITS

    MAX_IMAGE_BYTES = int(_INPUT_LIMITS["image_studio_bytes"])
except Exception:  # worker lancé hors du paquet backend
    MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_IMAGE_B64_CHARS = 4 * ((MAX_IMAGE_BYTES + 2) // 3)
MAX_WORKER_LINE_CHARS = MAX_IMAGE_B64_CHARS + 4096
_DRAIN_CHUNK = 64 * 1024

# Libellés exacts du README PaddleOCR-VL-1.6. PaddleX s'appuie sur les mêmes
# chaînes pour router une région de mise en page vers la bonne Specialized
# recognition, donc les reprendre telles quelles évite toute table de
# correspondance locale à maintenir.
PROMPTS = {
    "parse": "OCR:",
    "table": "Table Recognition:",
    "chart": "Chart Recognition:",
    "formula": "Formula Recognition:",
    "spotting": "Spotting:",
    "seal": "Seal Recognition:",
}

# Le processor.image_processor n'expose pas toujours `min_pixels` : on lit sa
# configuration réelle, avec la valeur officielle en repli.
MIN_PIXELS_DEFAUT = 112896
MAX_PIXELS_OCR = 1280 * 28 * 28
MAX_PIXELS_SPOTTING = 2048 * 28 * 28
SEUIL_AGRANDISSEMENT_SPOTTING = 1500

# Le cache KV est le levier de latence MESURÉ sur ce dépôt : sans lui,
# `generate` réévalue tout le préfixe à chaque jeton (≈5 tok/s sur une page
# scannée) ; avec lui, le décodage réutilise les clés/valeurs (≈57–65 tok/s),
# pour un texte identique. Les fichiers du modèle portent `"use_cache": false` :
# on ne les modifie pas, on transmet la valeur à CHAQUE appel pour que le
# comportement soit lisible dans le code et non hérité d'une config distante.
USE_CACHE_KV = True

_model = None
_processor = None
_layout = None
# Cause de l'échec de chargement du layout, conservée pour la rendre à
# detect_regions : un « non chargé » sans cause laisserait un appelant
# deviner (paddle_client.py : FAIL LOUD — modèle absent → RuntimeError explicite).
_layout_erreur: str | None = None
_lock = threading.Lock()


def _model_dir():
    from backend.config.paths import get_document_model_dir

    return str(get_document_model_dir(MODEL_NAME))


def _layout_dir():
    from backend.config.paths import get_document_model_dir

    return str(get_document_model_dir(LAYOUT_MODEL_NAME))


def _valeurs_pixels(task: str) -> tuple[int, int]:
    if task == "spotting":
        return MIN_PIXELS_DEFAUT, MAX_PIXELS_SPOTTING
    return MIN_PIXELS_DEFAUT, MAX_PIXELS_OCR


def charger_modele():
    """Charge PaddleOCR-VL sur le GPU courant. Le modèle reste résident."""
    global _model, _processor
    import torch
    from transformers import AutoModelForImageTextToText, AutoProcessor

    chemin = _model_dir()
    device = "cuda" if torch.cuda.is_available() else "cpu"
    dtype = torch.bfloat16 if device == "cuda" else torch.float32
    _model = (
        AutoModelForImageTextToText.from_pretrained(
            chemin,
            dtype=dtype,
            trust_remote_code=False,
        )
        .to(device)
        .eval()
    )
    _processor = AutoProcessor.from_pretrained(chemin, trust_remote_code=False)
    # Le cache KV est un paramètre de GÉNÉRATION : si la version installée ne
    # l'expose pas, on s'arrête ici plutôt que de servir des pages à ≈5 tok/s
    # en laissant croire que le réglage est actif (repli silencieux interdit).
    if USE_CACHE_KV and "use_cache" not in _model.generation_config.to_dict():
        raise RuntimeError(
            "cache KV demandé, mais la configuration de génération du modèle "
            "n'expose pas `use_cache` : la version de transformers installée "
            "ne peut pas l'activer."
        )
    return device


def charger_layout():
    """Charge PP-DocLayoutV3 pour la détection de régions. Optionnel.

    L'OCR de page entière n'en a pas besoin : le modèle transcrit la page. Le
    layout sert aux crops, pour ne faire transcrire qu'une région à la fois ou
    router une image vers la vision générale. Son absence ne doit donc pas
    empêcher l'OCR de fonctionner.
    """
    global _layout
    import torch
    from transformers import AutoImageProcessor, AutoModelForObjectDetection

    chemin = _layout_dir()
    device = "cuda" if torch.cuda.is_available() else "cpu"
    _layout = (
        AutoModelForObjectDetection.from_pretrained(
            chemin,
            trust_remote_code=False,
            dtype=torch.float32,
        )
        .to(device)
        .eval(),
        AutoImageProcessor.from_pretrained(chemin, trust_remote_code=False),
    )
    return device


def _charger_tout() -> str:
    device = charger_modele()
    global _layout, _layout_erreur
    try:
        charger_layout()
        _layout_erreur = None
    except Exception as exc:
        # Le layout est un complément, pas une condition : l'OCR de page entière
        # continue sans lui. La CAUSE est conservée : quand detect_regions sera
        # appelé, l'échec sera bruyant et nommera la raison exacte, jamais un
        # contournement silencieux (consigne 4.3).
        _layout = None
        _layout_erreur = f"{type(exc).__name__}: {exc}"
    return device


def _preparer_image(image, task: str):
    from PIL import Image

    image = image.convert("RGB")
    largeur, hauteur = image.size
    # Le spotting travaille sur des coordonnées normalisées : sous 1500 px il
    # perd les petits caractères, l'agrandissement est donc officiel.
    if (
        task == "spotting"
        and largeur < SEUIL_AGRANDISSEMENT_SPOTTING
        and hauteur < SEUIL_AGRANDISSEMENT_SPOTTING
    ):
        try:
            filtre = Image.Resampling.LANCZOS
        except AttributeError:
            filtre = Image.LANCZOS
        image = image.resize((largeur * 2, hauteur * 2), filtre)
    return image


def _inference(
    image,
    task: str,
    max_tokens: int,
    *,
    use_cache: bool = USE_CACHE_KV,
) -> str:
    """Transcrit une image ; `use_cache` active la réutilisation des clés/valeurs.

    Le cache est passé explicitement à `generate` et non lu depuis
    `generation_config.json`, qui porte `"use_cache": false`. Le laisser à
    `False` est un choix de mesure (comparaison de sortie), jamais un repli
    implicite : la valeur par défaut est celle de la production.
    """
    prompt = PROMPTS.get(task)
    if prompt is None:
        raise ValueError(f"tâche inconnue : {task} ({'|'.join(PROMPTS)})")
    image = _preparer_image(image, task)
    court, long = _valeurs_pixels(task)
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image", "image": image},
                {"type": "text", "text": prompt},
            ],
        }
    ]
    entrees = _processor.apply_chat_template(
        messages,
        add_generation_prompt=True,
        tokenize=True,
        return_dict=True,
        return_tensors="pt",
        images_kwargs={"size": {"shortest_edge": court, "longest_edge": long}},
    ).to(_model.device)
    sortie = _model.generate(
        **entrees,
        max_new_tokens=max_tokens,
        do_sample=False,
        use_cache=use_cache,
    )
    # On retire le jeton final ajouté par generate ; sans cela le texte
    # renvoyé se termine par un caractère parasite, trompeur pour l'appelant.
    return _processor.decode(
        sortie[0][entrees["input_ids"].shape[-1] :],
        skip_special_tokens=True,
    ).strip()


def _layout_regions(image_b64: str, seuil: float) -> list[dict]:
    """Régions de mise en page, triées dans l'ordre de lecture du document."""
    import io as _io

    from PIL import Image

    if _layout is None:
        raise RuntimeError(
            "PP-DocLayoutV3 indisponible : détection de régions refusée (échec "
            "bruyant, jamais de contournement silencieux — la page ne part pas "
            "à l'OCR en croisant les doigts). Cause du chargement : "
            f"{_layout_erreur or 'jamais tenté'}"
        )
    modele, processor = _layout
    image = Image.open(_io.BytesIO(_octets_image({"image_b64": image_b64}))).convert(
        "RGB"
    )
    entrees = processor(images=image, return_tensors="pt").to(modele.device)
    with torch_no_grad():
        brut = modele(**entrees)
    resultat = processor.post_process_object_detection(
        brut,
        threshold=seuil,
        target_sizes=[image.size[::-1]],
    )[0]
    regions = []
    for score, label, box in zip(
        resultat["scores"], resultat["labels"], resultat["boxes"]
    ):
        regions.append(
            {
                "label": modele.config.id2label[int(label)],
                "label_id": int(label),
                "score": round(float(score), 4),
                "box": [round(float(x), 2) for x in box.tolist()],
            }
        )
    regions.sort(key=lambda r: (r["box"][1], r["box"][0]))
    return regions


def torch_no_grad():
    import torch

    return torch.inference_mode()


def _octets_image(message: dict) -> bytes:
    """Décode `image_b64` en refusant AVANT `b64decode` ce qui dépasse.

    `validate=True` refuse les caractères hors alphabet base64 : sans lui, un
    payload corrompu était silencieusement nettoyé et passait pour une image.
    """
    brut = message.get("image_b64", "")
    if not isinstance(brut, str):
        raise ValueError("image_b64 doit être une chaîne base64")
    if len(brut) > MAX_IMAGE_B64_CHARS:
        raise ValueError(
            f"Image base64 trop volumineuse (maximum {MAX_IMAGE_BYTES // (1024 * 1024)} Mo)."
        )
    try:
        donnees = base64.b64decode(brut, validate=True)
    except Exception as exc:
        raise ValueError("Image base64 invalide") from exc
    if len(donnees) > MAX_IMAGE_BYTES:
        raise ValueError(
            f"Image trop volumineuse (maximum {MAX_IMAGE_BYTES // (1024 * 1024)} Mo)."
        )
    return donnees


def _lire_ligne_bornee(stream) -> tuple[str, bool]:
    """Lit une ligne JSON du worker sans accumuler au-delà de la borne.

    Même règle que l'IPC : une première lecture bornée, puis la queue est vidée
    par blocs et jetée. Le worker ne doit pas être un point d'entrée mémoire
    non borné.
    """
    ligne = stream.readline(MAX_WORKER_LINE_CHARS + 1)
    if not ligne:
        return "", False
    if ligne.endswith("\n"):
        if len(ligne) - 1 <= MAX_WORKER_LINE_CHARS:
            return ligne, False
        return ligne[:MAX_WORKER_LINE_CHARS], True
    if len(ligne) <= MAX_WORKER_LINE_CHARS:
        return ligne, False
    while True:
        reste = stream.readline(_DRAIN_CHUNK)
        if not reste or reste.endswith("\n"):
            break
    return ligne[:MAX_WORKER_LINE_CHARS], True


def _traiter(message: dict) -> dict:
    from PIL import Image

    operation = message.get("op", "parse")
    if operation == "layout":
        return {
            "id": message.get("id"),
            "ok": True,
            "regions": _layout_regions(
                message.get("image_b64", ""),
                float(message.get("threshold", 0.3)),
            ),
        }
    if operation not in PROMPTS:
        return {
            "id": message.get("id"),
            "ok": False,
            "error": f"tâche inconnue : {operation} ({'|'.join(PROMPTS)}|layout)",
        }
    image = Image.open(io.BytesIO(_octets_image(message)))
    texte = _inference(image, operation, int(message.get("max_tokens", 8192)))
    return {"id": message.get("id"), "ok": True, "text": texte}


def _repondre(charge: dict) -> None:
    """Écrit une réponse JSONL suivie d'un saut de ligne, puis vide la sortie.

        Le saut de ligne n'est pas cosmétique : le client lit ligne par ligne, et
        `readline` sur un flux sans Terminator reste bloqué jusqu'à l'arrêt du
        worker. L'absence de `
    ` transforme chaque réponse réussie en timeout.
    """
    sys.stdout.write(json.dumps(charge, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _main() -> int:
    try:
        device = _charger_tout()
    except Exception as exc:
        _repondre({"ready": False, "error": f"{type(exc).__name__}: {exc}"})
        return 1
    _repondre({"ready": True, "model": MODEL_NAME, "device": device})
    while True:
        ligne, trop_longue = _lire_ligne_bornee(sys.stdin)
        if not ligne:
            break
        if trop_longue:
            _repondre(
                {
                    "id": None,
                    "ok": False,
                    "error": (
                        "ligne trop volumineuse "
                        f"(maximum {MAX_WORKER_LINE_CHARS} caractères)"
                    ),
                }
            )
            continue
        ligne = ligne.strip()
        if not ligne:
            continue
        try:
            message = json.loads(ligne)
        except Exception as exc:
            _repondre({"id": None, "ok": False, "error": f"JSON invalide : {exc}"})
            continue
        identifiant = message.get("id")
        try:
            with _lock:
                reponse = _traiter(message)
        except Exception as exc:
            reponse = {
                "id": identifiant,
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
        _repondre(reponse)
    return 0


if __name__ == "__main__":
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    sys.exit(_main())
