# Prompt OCR — extrait texte et tableaux d'un document
# Modèle : GLM-OCR (8K, 0.9B), température 0.0
# Protocole marqueurs fermés GLM-OCR — pas de prompt libre (ticket #19721)
# Marqueur : Text Recognition: (défaut) ou Table Recognition: (si requête mentionne tableau/table)

import re

# Marqueurs officiels GLM-OCR (entraînement encoder-décodeur GLM-V+MTP)
OCR_TEXT_MARKER = "Text Recognition:"
OCR_TABLE_MARKER = "Table Recognition:"

_TABLE_KEYWORDS = (
    "tableau", "table ", "table?", "tabuler", "grille", "csv",
    "résumé structuré", "données structurées", "extrait.*table",
)

def _is_table_request(query: str) -> bool:
    """Détection de tables dans la query utilisateur."""
    lower = query.lower()
    return any(kw in lower for kw in _TABLE_KEYWORDS)

def build_system_ocr(query: str, *, request_table: bool = None) -> str:
    """Construit le prompt OCR avec le marqueur GLM-OCR adéquat.
    - request_table explicite > détection auto dans query > default Text
    - La consigne de fidélite et de langue reste du côté du générateur (brief)
    """
    use_table = request_table if request_table is not None else _is_table_request(query)
    marker = OCR_TABLE_MARKER if use_table else OCR_TEXT_MARKER
    return (
        f"Tu es GLM-OCR, un OCR technique déterministe.\n"
        f"{marker} Extrais le contenu de ce document.\n\n"
        "Règles:\n"
        "- Fidélite absolue (ne reformules jamais)\n"
        "- Si une zone est illisible, indique [ILLISIBLE]\n"
        "- Ne rajoute rien, ne résume pas\n"
        "- Conserve la langue du document (pas de traduction)\n"
    )

# CONSTANT par défaut pour backward compat avec loop.py imports
SYSTEM_OCR = build_system_ocr("")
