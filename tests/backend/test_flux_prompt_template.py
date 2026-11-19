"""Checklist fiabilité prompt Flux (garde-fous : BOS unique, generation prompt, EOS, pad 512).

Ce que le spike T2I exige du templating (régression = bruit) :
- pas de BOS fantôme ajouté par l'encodeur ;
- add_generation_prompt présent (le modèle continue après `<|im_start|>assistant`) ;
- `<|im_end|>` (151645) closant les tours terminés ;
- pad 512 avec `<|endoftext|>` (151643), jamais de dépassement.
"""

from pathlib import Path

import pytest

jinja2 = pytest.importorskip("jinja2")
tokenizers = pytest.importorskip("tokenizers")

TOK_DIR = Path(__file__).resolve().parents[2] / "models" / "flux2-klein" / "tokenizer"
# (poids git-ignorés, convention projet models/ ; le moteur les résout via
#  backend.image_engine.core.paths — ici chemin repo direct car les tests
#  tournent depuis la racine projet)
BOS, EOS, PAD_LEN = 151643, 151645, 512

# Le tokenizer (template + vocabulaire) est un POIDS de modèle : git-ignoré, il
# n'est pas livré dans un checkout public. Sans lui, ces tests ne mesurent rien
# — ils SAUTENT nommément au lieu d'échouer en `TemplateNotFound`. S'il est
# livré, ils s'exécutent normalement.
MODELE_LIVRE = (TOK_DIR / "chat_template.jinja").is_file() and (
    TOK_DIR / "tokenizer.json"
).is_file()
pytestmark = pytest.mark.skipif(
    not MODELE_LIVRE,
    reason="poids de modèle non livrés — test ignoré sur un checkout public",
)


IM_START = 151644  # <|im_start|> : le template ouvre par lui, SANS BOS explicite


def _render(messages):
    from jinja2 import Environment, FileSystemLoader

    tpl = Environment(loader=FileSystemLoader(str(TOK_DIR))).get_template(
        "chat_template.jinja"
    )
    return tpl.render(
        messages=messages,
        add_generation_prompt=True,
        enable_thinking=False,
        bos_token="<|endoftext|>",
        eos_token="<|im_end|>",
        pad_token="<|endoftext|>",
    )


def _encode(text):
    from tokenizers import Tokenizer

    return Tokenizer.from_file(str(TOK_DIR / "tokenizer.json")).encode(text).ids


def test_pas_de_bos_fantome():
    # Le template n'émet pas de BOS (il ouvre par <|im_start|>) : l'encodeur ne doit
    # pas en ajouter un — un BOS fantôme dupliquerait le conditionnement.
    ids = _encode(_render([{"role": "user", "content": "un chat"}]))
    assert ids[0] == IM_START, "le template doit ouvrir par <|im_start|>"
    assert ids.count(BOS) == 0, f"BOS fantôme ajouté par l'encodeur ({ids.count(BOS)}×)"


def test_generation_prompt_present():
    text = _render([{"role": "user", "content": "un chat"}])
    assert "<|im_start|>assistant" in text, "add_generation_prompt manquant"
    assert text.endswith("<|im_start|>assistant\n<think>\n\n</think>\n\n"), (
        "queue de génération inattendue"
    )


def test_eos_closant_tour_assistant():
    ids = _encode(
        _render(
            [
                {"role": "user", "content": "un chat"},
                {"role": "assistant", "content": "voici"},
            ]
        )
    )
    assert EOS in ids, "un tour terminé doit être clos par <|im_end|>"
    assert ids[-1] != EOS, "pas d'EOS final : la génération continue après le think"


def test_pad_512():
    ids = _encode(_render([{"role": "user", "content": "un chat roux dans un jardin"}]))
    assert len(ids) < PAD_LEN
    padded = (ids + [BOS] * (PAD_LEN - len(ids)))[:PAD_LEN]
    assert len(padded) == PAD_LEN and all(i == BOS for i in padded[len(ids) :])
