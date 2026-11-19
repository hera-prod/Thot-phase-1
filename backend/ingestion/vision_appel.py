# ingestion/vision_appel.py — Couche d'appel au modèle de vision générale (LFM2.5-VL-3B)
#
# POURQUOI CE MODULE : la vision générale vit derrière LE ModelManager unique du
# sidecar (backend/main.py), et « pas de singleton manager exposé »
# (backend/inference/compaction.py : tout accès direct global est mort). Le fil
# d'ingestion en arrière-plan n'a donc aucun manche sur le modèle, et il ne faut
# pas inventer une seconde instance : deux états chargés se battraient pour la
# VRAM. C'est la couche d'entrée qui sert le modèle (main.py) qui DEMANDE ici
# son closure d'appel — le même motif que qualification.py (« closure fournie
# par la couche d'appel »).
#
# ORDONNANCEMENT (mecanisme — pdf.md §2.1) : « son admission respecte
# l'ordonnancement et ne lance pas de traitement de modèle lourd en concurrence
# avec un tour actif ». Si un tour interactif est actif, la vision d'INGESTION
# cède immédiatement le modèle : `couche_managere` lève `ResourceBusy` avant
# toute conversion d'image et avant `manager.use_model`. L'appelant diffère et
# replanifie (`figures_worker` → `_differe` ; `attachment_ingest` → `depose`).
# Le chat, lui, passe par `loop.py`, pas par ce module : ce yielding ne coupe
# jamais la vision d'un tour interactif.
#
# Sinon l'appel passe par manager.use_model, qui prend le bail « model » du
# coordinateur de ressources : l'ingestion ATTEND son tour (attente réglée,
# pas de coupure en vol). Si le bail ne se prend pas (tour actif long)
# → ResourceBusy, remontée nommée : l'appelant a une figure repérée et un
# repère, et note la limite dans le Markdown (clean break partiel, pdf.md §4.6 :
# « Distinguer format invalide, …, rendu échoué, texte dégradé, … »).
from __future__ import annotations

import base64
import io
from typing import Any, Callable

AppelVision = Callable[[str, bytes], str]

_couche: AppelVision | None = None


class VisionIndisponible(RuntimeError):
    """Aucune couche d'appel enregistrée : la vision générale est injoignable.

    Un état, pas une panne : le sidecar n'est pas démarré, ou est un process de
    tests. Le manque est nommé, jamais un crop réexaminé à demi ni un crash de
    l'ingestion complète pour une figure.
    """


def enregistrer_couche_appel(couche: AppelVision) -> None:
    """Pose LA couche d'appel de ce process (ré-enregistrement = remplacement)."""
    global _couche
    if couche is None:
        raise ValueError("couche d'appel vision vide : refuse une inscription muette")
    _couche = couche


def _couche_actuelle() -> AppelVision:
    if _couche is None:
        raise VisionIndisponible(
            "vision générale indisponible : aucune couche d'appel LFM2.5-VL-3B "
            "enregistrée dans ce process (main.py est absent ou n'a pas encore "
            "proposé la sienne) — la limite nommée, documentée, reste tracée"
        )
    return _couche


def appeler_vision_generale(consigne: str, png_bytes: bytes) -> str:
    """Point d'entrée unique de l'ingestion vers la vision générale. Pur (hors appel).

    Lève `VisionIndisponible` si la couche n'est pas enregistrée ; les erreurs
    dues au modèle traversent avec leur cause réelle, jamais absorbées : un
    réexamen manqué tient dans le bloc de figure documenté (pdf.md §4.6).
    """
    if not consigne or not consigne.strip():
        raise ValueError("consigne de vision vide : aucune demande à poser")
    if not png_bytes:
        raise ValueError("pixels vides : aucune image à transmettre à la vision")
    return _couche_actuelle()(consigne, png_bytes)


def couche_managere(manager: Any) -> AppelVision:
    """Construit la closure d'appel portant le ModelManager. Une fois, nommée.

    Les pixels partent réellement au modèle : PNG → RGB → JPEG quality 88 en
    data URL — même voie que la qualification image (loop.py
    `_qualifier_image_par_vision`), conforme à la pseudo-vision outil
    (backend/tools/README.md §2, code réel ligne 45 : « image =
    prepare_vision_image(blob 1536px JPEG88, tracé NaFlex 512) » — le
    redimensionnement 1536 px est inutile ici : le crop de figure sort déjà
    petit). Temperature 0.2 =
    recommandation du profil VL fournisseur (lfm2.5-vl-3b.md §4.3, JSON réel :
    « "temperature": 0.2, "top_k": 50 ») ; distinct du profil du 8B (fiche :
    « Ces valeurs ne sont pas celles du modèle 8B : les profils doivent rester
    séparés »).

    YIELDING : si un tour interactif est actif (`activity.tour_actif()`), la
    closure lève `ResourceBusy` AVANT d'ouvrir l'image et AVANT
    `manager.use_model` : l'ingestion cède le modèle au chat, et son appelant
    la replanifie sans consommer d'essai.
    """
    from PIL import Image

    def couche(consigne: str, png_bytes: bytes) -> str:
        from backend.services import activity

        if activity.tour_actif():
            from backend.inference.resource_coordinator import ResourceBusy

            raise ResourceBusy(
                "RESOURCE_BUSY:model (tour interactif actif — vision "
                "d'ingestion différée)"
            )
        image = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        album = io.BytesIO()
        image.save(album, format="JPEG", quality=88)
        b64 = base64.b64encode(album.getvalue()).decode("utf-8")
        data_url = f"data:image/jpeg;base64,{b64}"
        with manager.use_model("LFM2.5-VL-3B", has_image=True, kind="ingestion") as vlm:
            reponse = vlm.create_chat_completion(
                messages=[
                    {"role": "system", "content": consigne},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": consigne},
                            {"type": "image_url", "image_url": {"url": data_url}},
                        ],
                    },
                ],
                temperature=0.2,
                max_tokens=512,
            )
            texte = (reponse["choices"][0]["message"]["content"] or "").strip()
            if not texte:
                raise VisionIndisponible(
                    "vision générale : réponse vide (b64_len="
                    f"{len(b64)}) — aucun texte rendu pour ce crop"
                )
            return texte

    return couche
