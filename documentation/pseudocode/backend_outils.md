# Pseudo-code — Outils

> Phase 1. Ce fichier couvre le **registre d'outils**, la **classe de base** et les **outils de vision / lecture**.

---

## 1. Registre

```python
[Phase 1]
# tools/registry.py — enregistre les outils disponibles pour l'orchestrateur.
# NOTE : en phase 1, 4 outils seulement. Pas de système de permissions (viendra phase 2+).
class ToolRegistry:
    def register(self, tool): ...      # ⚠️ refuse un doublon de nom
    def get(self, name): ...           # lève KeyError si inconnu
    def definitions(self): ...         # liste au format "function calling" pour le LLM

def get_default_registry():
    r = ToolRegistry()
    r.register_many([VisionGeneralTool(), VisionOCRTool(), GetSourceTool(), ReadTextTool()])
    return r
```

---

## 2. Classe de base

```python
[Phase 1]
# tools/base.py — contrat commun à tous les outils.
class BaseTool:
    name: str                 # identifiant unique ("vision", "doc_ocr", ...)
    description: str          # description injectée au LLM pour choisir l'outil
    def definition(self) -> dict:   # schéma des arguments (au format OpenAI function)
    def execute(self, args: dict) -> dict:   # renvoie {"ok": bool, "result": str}
    # ⚠️ "execute" doit TOUJOURS retourner un dict, jamais écrire sur stdout.
```

---

## 3. Routage des pièces jointes

```python
[Phase 1]
# NOTE : routage DÉTERMINISTE avant de laisser le LLM choisir.
# Un PDF ➜ doc_ocr ; une image ➜ vision ; une demande "lis le texte" ➜ doc_ocr.
def select_attachment_tool(image_paths):
    if any(p.endswith(".pdf") for p in image_paths): return "doc_ocr"
    return "vision"
```

---

## 4. Outil de vision générale

```python
[Phase 1]
class VisionGeneralTool(BaseTool):
    name = "vision"
    # NOTE : utilise LFM2.5-VL-3B (chargé paresseusement via ModelManager).
    def execute(self, args):
        img = resolve_and_validate(args["image_path"])   # ⚠️ valider le chemin (le LLM peut l'halluciner)
        prepared = preprocess_image(img)                  # EXIF + max 1536px + JPEG 88 (réduit VRAM)
        with manager.use_model("LFM2.5-VL-3B") as vlm:    # ➜ décharge le LLM, charge le VL
            out = vlm.create_chat_completion(...)         # image envoyée en data URL
            return {"ok": True, "result": out}
```

---

## 5. Outil OCR de document

```python
[Phase 1]
class VisionOCRTool(BaseTool):
    name = "doc_ocr"
    # NOTE : utilise GLM-OCR pour lire texte, tableaux, PDF, factures.
    def execute(self, args):
        path = args["pdf_path"]
        # traitement page par page (PyMuPDF, 200 DPI) pour éviter l'explosion du KV.
        pages = render_pages(path)
        with manager.use_model("GLM-OCR") as ocr:
            text = "".join(ocr_page(page) for page in pages)
            return {"ok": True, "result": text}
```

---

## 6. Outils de consultation des sources

```python
[Phase 1]
class GetSourceTool(BaseTool):
    name = "get_source"
    # NOTE : liste les fichiers déposés dans la conversation (manifest), sans les ouvrir.
    # ➜ permet au LLM de savoir ce qui est disponible avant de lire.

class ReadTextTool(BaseTool):
    name = "read_text"
    # NOTE : lit un fichier TEXTE (markdown, txt) — ⚠️ différent de doc_ocr qui "voit" le document.
```

---

## Points de vigilance

- **`vision` (généraliste) ≠ `doc_ocr` (texte)** : deux modèles différents.
- **`read_text` (extraire du texte) ≠ TTS (lire à voix haute)** : le TTS arrive en phase 3.
- **Valider les chemins** : le modèle peut inventer un `image_path` ; ne l'utiliser que s'il correspond à un fichier réellement fourni.

---

## Correspondance

- [Document — Phase 1](../phase_1_socle_et_vision.md)
- Avant : [backend_agent_loop.md](backend_agent_loop.md) · Après : [backend_db.md](backend_db.md)