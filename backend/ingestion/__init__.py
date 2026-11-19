# ingestion — Pipeline brouillon (PaddleOCR-VL + gate 3 issues, sans DocLayout pour l'instant)
# Moteur livré : PaddleOCR-VL-1.6 (safetensors local) via worker transformers dédié.
# GLM-OCR (GGUF Vision-document, jamais livré) est abandonné — voir notes
# dans paddle_client.py et gate.py pour les replis documentés (pas de paddle, pas de
# pdfplumber/Camelot installés ; gate heuristique en attendant PP-DocLayoutV3).
