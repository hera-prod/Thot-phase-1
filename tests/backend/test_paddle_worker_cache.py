"""Le worker Paddle active le cache KV dans le chemin de PRODUCTION.

Le défaut verrouillé ici est une régression de performance silencieuse : les
fichiers du modèle portent `"use_cache": false`, et sans transmission explicite
`generate` réévalue tout le préfixe à chaque jeton (≈5 tok/s au lieu de
≈57–65). La sortie reste identique, donc aucun test de contenu ne verrait la
régression ; seul un test sur les arguments de génération la voit.

Aucun poids, aucun GPU : le modèle et le processor sont remplacés par des
doubles qui MÉMORISENT les arguments reçus. Retirer `use_cache=True` de
`_inference` fait échouer ces tests.
"""

from __future__ import annotations

import pytest


class _Ids:
    """Forme minimale de `input_ids` : `_inference` ne lit que `.shape[-1]`."""

    shape = (1, 3)


class _Entrees(dict):
    """Entrées tokenisées : `_inference` appelle `.to(device)` sur le dict."""

    def to(self, device):
        return self


class _ProcesseurFactice:
    def apply_chat_template(self, *args, **kwargs):
        return _Entrees(input_ids=_Ids())

    def decode(self, tokens, skip_special_tokens=True):
        return "texte factice "


class _ModeleFactice:
    """Mémorise les kwargs de `generate` : c'est l'objet du test."""

    device = "cpu"

    def __init__(self) -> None:
        self.appels: list[dict] = []

    def generate(self, **kwargs):
        self.appels.append(kwargs)
        # 3 jetons d'entrée, 2 jetons générés : le décodage reçoit la tranche.
        return [[10, 11, 12, 20, 21]]


@pytest.fixture()
def worker_factice(monkeypatch):
    from backend.ingestion import paddle_worker

    modele = _ModeleFactice()
    monkeypatch.setattr(paddle_worker, "_model", modele)
    monkeypatch.setattr(paddle_worker, "_processor", _ProcesseurFactice())
    monkeypatch.setattr(paddle_worker, "_preparer_image", lambda image, task: image)
    return paddle_worker, modele


def test_le_chemin_de_production_demande_le_cache_kv(worker_factice):
    """Sans argument explicite, `_inference` est le chemin de production."""
    worker, modele = worker_factice

    texte = worker._inference(object(), "parse", 8192)

    assert texte == "texte factice"
    assert modele.appels, "generate n'a pas été appelé"
    kwargs = modele.appels[0]
    assert kwargs["use_cache"] is True, (
        "le worker régénère tout le préfixe à chaque jeton : la page scannée "
        "passe de ≈11 s à ≈123 s sans que la sortie change"
    )
    assert kwargs["max_new_tokens"] == 8192, "le budget de production a changé"
    assert kwargs["do_sample"] is False


def test_un_message_ocr_du_worker_active_le_cache(worker_factice):
    """Le protocole JSONL réel mène bien à `generate(use_cache=True)`.

    Ce test part de `_traiter` — ce que le worker exécute pour une ligne reçue —
    et non directement de `_inference` : il verrouille le maillon entre le
    message de production et l'appel de génération.
    """
    import base64
    import io

    from PIL import Image

    worker, modele = worker_factice
    tampon = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(tampon, format="PNG")

    reponse = worker._traiter(
        {
            "id": 7,
            "op": "parse",
            "image_b64": base64.b64encode(tampon.getvalue()).decode(),
            "max_tokens": 8192,
        }
    )

    assert reponse == {"id": 7, "ok": True, "text": "texte factice"}
    assert modele.appels[0]["use_cache"] is True


def test_le_cache_reste_desactivable_pour_la_mesure(worker_factice):
    """La comparaison avant/après exige un chemin sans cache, explicite."""
    worker, modele = worker_factice

    worker._inference(object(), "parse", 8192, use_cache=False)

    assert modele.appels[0]["use_cache"] is False


def test_la_tache_inconnue_echoue_avant_toute_generation(worker_factice):
    """Guard clause : une tâche inconnue ne doit pas atteindre `generate`."""
    worker, modele = worker_factice

    with pytest.raises(ValueError, match="tâche inconnue"):
        worker._inference(object(), "inconnue", 8192)

    assert not modele.appels
