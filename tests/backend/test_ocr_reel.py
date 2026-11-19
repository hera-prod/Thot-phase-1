"""Tests du pipeline documentaire : PaddleOCR-VL-1.6 et PP-DocLayoutV3.

POURQUOI CES TESTS EXISTENT : le dépôt d'une pièce jointe réussissait toujours,
et l'échec n'apparaissait qu'au moment de la lecture, sans lien visible entre
les deux. L'utilisateur voyait un dépôt valide et une analyse muette. Ces tests
rendent la panne détectable là où le texte doit sortir, et non dans un message
d'erreur tardif.

Ils sont markés `slow` : ils chargent 1,8 Go de poids sur le GPU. Ils ne sont
pas exécutés par défaut pour que la suite reste rapide, mais ils doivent passer
avant de considérer l'ingestion documentaire opérationnelle.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parents[2]
FIXTURE = RACINE / "tests" / "fixtures" / "images" / "image_test.png"

pytestmark = pytest.mark.slow


def _exiger_modeles() -> None:
    pytest.importorskip("torch", reason="torch absent")
    pytest.importorskip("transformers", reason="transformers absent")
    from backend.config.paths import get_document_model_dir

    for nom in ("PaddleOCR-VL-1.6", "PP-DocLayoutV3"):
        if not get_document_model_dir(nom).is_dir():
            pytest.skip(f"modèle absent : {nom}")


def test_fixture_depose_une_vraie_page() -> None:
    """La fixture doit être une page A4 dense, pas une image de test triviale.

    Une page plate ne déclencherait pas les difficultés de mise en page qui
    font échouer l'OCR ; le test passerait alors pour une mauvaise raison.
    """
    from PIL import Image

    assert FIXTURE.exists(), f"fixture absente : {FIXTURE}"
    with Image.open(FIXTURE) as im:
        largeur, hauteur = im.size
    assert hauteur > largeur, "l'image doit être en portrait"
    assert hauteur >= 2000, "page trop petite pour exercer la mise en page"


def test_worker_demarre_sans_dependre_d_un_venv() -> None:
    """Le worker doit démarrer avec le Python courant.

    Un venv était prévu pour contourner une incompatibilité de version qui
    n'existait pas. Le conserver aurait réintroduit un point de rupture placé
    hors du dépôt, donc perdu au redémarrage.
    """
    from backend.ingestion import paddle_client

    assert "paddleocr-env" not in paddle_client.__file__
    assert not hasattr(paddle_client, "VENV_PYTHON"), (
        "le client ne doit plus exiger de venv dédié : l'implémentation native "
        "de transformers suffit"
    )
    assert Path(paddle_client.WORKER_SCRIPT).is_file()


def test_disponibilite_signale_les_deux_modeles() -> None:
    """La sonde doit dire ce qui manque, jamais mentir par optimisme."""
    from backend.ingestion.paddle_client import check_available

    ok, detail = check_available()
    if not ok:
        pytest.skip(f"modèles non installés : {detail}")
    assert "PaddleOCR-VL-1.6" in detail
    assert "layout" in detail, "la sonde doit se prononcer sur PP-DocLayoutV3"


def test_layout_detecte_les_regions_dune_page_reelle() -> None:
    """PP-DocLayoutV3 doit renvoyer des régions étiquetées et ordonnées."""
    _exiger_modeles()
    from backend.ingestion.paddle_client import detect_regions

    regions = detect_regions(FIXTURE.read_bytes())
    assert regions, "aucune région détectée sur une page qui en contient"
    for region in regions:
        assert region["label"], "région sans étiquette"
        assert 0.0 <= region["score"] <= 1.0, f"score hors bornes : {region}"
        assert len(region["box"]) == 4, f"boîte mal formée : {region}"
    haut = [r["box"][1] for r in regions]
    assert haut == sorted(haut), "les régions doivent suivre l'ordre de lecture"


def test_ocr_transcrit_la_page_reelle() -> None:
    """PaddleOCR-VL-1.6 doit rendre du texte, pas un motif répété.

    Une sortie dégénérée est le symptôme d'un modèle qui s'exécute sans voir
    l'image : prompt mal construit, ou positions manquantes. C'est précisément
    le mode de panne que ce test doit attraper, parce qu'il ne lève aucune
    erreur et laisserait croire à une réussite.
    """
    _exiger_modeles()
    from backend.ingestion.paddle_client import parse_png

    texte = parse_png(FIXTURE.read_bytes())
    assert texte.strip(), "l'OCR a rendu une chaîne vide"
    assert len(texte) > 200, (
        f"texte trop court pour une page dense ({len(texte)} caractères)"
    )
    # Une boucle de génération produit un motif régulier, jamais de la prose.
    mots = texte.split()
    if len(mots) > 20:
        ratio = len(set(mots)) / len(mots)
        assert ratio > 0.15, (
            f"sortie dégénérée : {len(set(mots))} mots distincts sur {len(mots)}"
        )
    for attendu in ("AIST", "DUERP", "risques"):
        assert attendu.lower() in texte.lower(), (
            f"« {attendu} » absent de la transcription"
        )


def test_ocr_avec_et_sans_cache_kv_rend_le_meme_texte() -> None:
    """Le cache KV est une optimisation de LATENCE, pas de contenu.

    Deux inférences sur la même page, l'une avec `use_cache=False`, l'autre
    avec `use_cache=True` : le texte doit être identique au caractère près.
    Si un jour le cache change la sortie, il faut le savoir avant de l'activer
    en production — d'où cette comparaison, marquée `slow` comme le reste du
    module puisqu'elle charge réellement le modèle.
    """
    _exiger_modeles()
    import torch
    from PIL import Image

    from backend.ingestion import paddle_worker

    paddle_worker.charger_modele()
    try:
        with Image.open(FIXTURE) as source:
            image = source.convert("RGB")
        sans_cache = paddle_worker._inference(image, "parse", 8192, use_cache=False)
        avec_cache = paddle_worker._inference(image, "parse", 8192, use_cache=True)
        assert sans_cache.strip(), "l'OCR sans cache a rendu une chaîne vide"
        assert sans_cache == avec_cache, (
            "le cache KV change la transcription : ne pas l'activer par défaut"
        )
    finally:
        paddle_worker._model = None
        paddle_worker._processor = None
        torch.cuda.empty_cache()


def test_ocr_pour_table_utilise_le_prompt_adapte() -> None:
    """Une table doit partir avec le bon prompt, pas celui du texte courant.

    PaddleX route chaque région selon son étiquette de mise en page ; un prompt
    unique transcrit le texte mais perd la structure des tableaux.
    """
    _exiger_modeles()
    from backend.ingestion.paddle_worker import PROMPTS

    assert PROMPTS["parse"] == "OCR:"
    assert PROMPTS["table"] == "Table Recognition:"
    for tache in ("chart", "formula", "spotting", "seal"):
        assert PROMPTS[tache].endswith(":"), f"prompt mal formé pour {tache}"


def test_prompt_officiel_est_utilise_et_non_construit_a_la_main() -> None:
    """Le prompt doit venir de apply_chat_template.

    Un prompt écrit à la main oublie l'expansion du jeton image en autant de
    placeholders que la grille de patchs : le modèle s'exécute et rend une
    sortie illisible, sans aucune erreur. C'est le mode de panne le plus
    trompeur que nous ayons rencontré, donc il est verrouillé ici.
    """
    from backend.ingestion import paddle_worker

    source = Path(paddle_worker.__file__).read_text(encoding="utf-8")
    assert "apply_chat_template" in source
    assert "<|user|>" not in source, (
        "<|user|> n'existe pas dans le vocabulaire de PaddleOCR-VL : "
        "un prompt construit à la main produit du texte dégénéré"
    )


def test_chaque_reponse_worker_tient_sur_une_ligne() -> None:
    """Le protocole exige un saut de ligne par message.

    Sans lui, `readline` du client reste bloqué jusqu'à l'arrêt du worker :
    une réponse parfaitement correcte se transforme en timeout.
    """
    from backend.ingestion import paddle_worker

    source = Path(paddle_worker.__file__).read_text(encoding="utf-8")
    assert '"\\n"' in source or "'\\n'" in source
    assert "sys.stdout.write" in source
    assert "json.dump(reponse" not in source, "json.dump n'écrit aucun séparateur"
