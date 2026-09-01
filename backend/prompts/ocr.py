# Prompt OCR — extrait texte et tableaux d'un document
# Modèle : GLM-OCR (8K, 0.9B), température 0.0
# Protocole marqueurs fermés GLM-OCR — pas de prompt libre (ticket #19721)
# Correctif Réponses.md n°6 : contrat stable *, task

# Marqueurs officiels GLM-OCR (entraînement encoder-décodeur GLM-V+MTP)
OCR_TEXT_MARKER = "Text Recognition:"
OCR_TABLE_MARKER = "Table Recognition:"
OCR_FORMULA_MARKER = "Formula Recognition:"

def resolve_ocr_task(query: str) -> str:
    text = query.lower()
    if any(word in text for word in ("tableau", "table", "colonnes")):
        return "table"
    if any(word in text for word in ("formule", "équation", "math")):
        return "formula"
    return "text"

def build_system_ocr(*, task: str) -> str:
    if task == "table":
        marker = OCR_TABLE_MARKER
    elif task == "formula":
        marker = OCR_FORMULA_MARKER
    else:
        marker = OCR_TEXT_MARKER
    return (
        f"Tu es GLM-OCR, un OCR technique déterministe.\n"
        f"{marker} Extrais le contenu de ce document.\n\n"
        "Règles:\n"
        "- Fidélite absolue (ne reformules jamais)\n"
        "- Si une zone est illisible, indique [ILLISIBLE]\n"
        "- Ne rajoute rien, ne résume pas\n"
        "- Conserve la langue du document (pas de traduction)\n"
    )

# Compat : SYSTEM_OCR utilisé si aucun contexte query
SYSTEM_OCR = build_system_ocr(task="text")
