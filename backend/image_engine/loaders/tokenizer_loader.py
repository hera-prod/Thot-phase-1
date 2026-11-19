"""Chargement tokenizer Qwen3."""
try:
    from transformers import AutoTokenizer
except Exception:
    # transformers casse parfois son import (ex : scipy système compilé pour numpy 1.x
    # sous numpy 2.x, 22/09/2026) — le text_encoder natif n'en a pas besoin.
    AutoTokenizer = None


def load_tokenizer(tokenizer_path: str):
    """Charge le tokenizer depuis un dossier local."""
    if AutoTokenizer is None:
        raise RuntimeError("transformers indisponible (import cassé) — utilise le text_encoder natif.")
    tok = AutoTokenizer.from_pretrained(tokenizer_path, trust_remote_code=True)
    # Qwen3 n'a pas de pad_token par defaut -> utiliser eos
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    return tok
