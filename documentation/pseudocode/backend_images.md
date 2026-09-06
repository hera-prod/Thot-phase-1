# Pseudo-code — Génération d'images (Phase 2)

> Phase 2. Ce fichier couvre l'**outil de génération d'images** (FLUX.2 Klein 9B) et la **galerie**.

---

## 1. L'outil de génération

```python
[Phase 2]
# tools/generate_image.py — nouvel outil enregistré dans le registre.
class GenerateImageTool(BaseTool):
    name = "generate_image"
    # NOTE : MÊME grammaire que les outils de vision (phase 1). Rien ne change côté protocole.
    def execute(self, args):
        prompt = args["prompt"]
        ref_ids = args.get("reference_ids", [])      # ⚠️ édition multi-références (jusqu'à 4)

        # ⚠️ swap séquentiel : décharger le LLM, charger la diffusion, régénérer le LLM ensuite.
        with manager.use_model("FLUX.2-Klein-9B") as diffusion:
            images = diffusion.generate(
                prompt=prompt,
                references=resolve_paths(ref_ids),   # ➜ résout les identifiants en chemins locaux
                aspect_ratio=args.get("aspect_ratio", "1:1"),
                progress_callback=emit_progress,      # ➜ jauge de diffusion dans l'UI
            )

        # ➜ enregistre le résultat : fichier sur disque + ligne "Source"/"Artifact" (catégorie image).
        results = [ingest_generated_image(img) for img in images]
        return {"ok": True, "result": results}
```

---

## 2. Résolution des références

```python
[Phase 2]
# NOTE : on ne passe JAMAIS d'image brute en base64 dans l'IPC.
# L'orchestrateur donne des IDENTIFIANTS ; le backend résout les chemins localement.
def resolve_paths(ref_ids):
    return [lookup_artifact(rid).file_path for rid in ref_ids]
```

---

## 3. Jauge de progression

```python
[Phase 2]
# NOTE : la diffusion se fait en étapes (encode, échantillonnage, décodage VAE).
# Émettre des événements de progression pour afficher une jauge (4 étapes typiques).
def emit_progress(step, total):
    emit_event("generation_progress", {"step": step, "total": total})
```

---

## 4. La galerie (backend)

```python
[Phase 2]
# NOTE : la galerie est une REQUÊTE SQL sur la table des artefacts, filtrée par catégorie.
def list_gallery(category=None):
    # ➜ renvoie les artefacts (id, catégorie, chemin, vignette, date) pour l'UI.
    # ⚠️ dès phase 2, la catégorie existe : en phase 3 la galerie accueillera aussi PDF/CSV/Markdown.
```

---

## Points de vigilance

- **Essai en génération ≠ édition** : l'édition exige de transmettre les références.
- **Cohabitation interdite** : FLUX ne doit jamais être chargé en même temps que le LLM.
- **Réutiliser la grammaire d'outils** de phase 1 ; ne pas inventer un second protocole.

---

## Correspondance

- [Document — Phase 2](../phase_2_generation_image.md)
- Annexe : [03 — FLUX.2 Klein](../annexes/03_flux_klein_generation.md)
- Suite : [backend_audio_documents.md](backend_audio_documents.md)