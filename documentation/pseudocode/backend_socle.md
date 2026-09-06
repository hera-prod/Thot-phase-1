# Pseudo-code — Socle (IPC, main, ModelManager)

> Phase 1. Ce fichier couvre le **démarrage du sidecar**, le **bus IPC JSON Lines** et le **gestionnaire de modèles** (chargement/déchargement séquentiel).

---

## 1. Point d'entrée `main`

```python
[Phase 1]
# main.py — point d'entrée du sidecar Python (spawné par Tauri).
# NOTE : TOUS les logs vont sur stderr. stdout est RÉSERVÉ au protocole JSON Lines.
# ⚠️ Ne JAMAIS écrire avec print() sur stdout : cela corromprait les messages lus par Tauri.

def main():
    manager = ModelManager()              # détecte le GPU, initialise le gardien des modèles
    registry = get_default_registry()     # enregistre les outils phase 1
    db = init_database()                  # crée/ouvre la base SQLite (voir backend_db)

    emit_event("backend_ready", {"backend": manager.backend_name})
    # ➜ signal à l'UI que le sidecar est prêt (avec le backend utilisé : cuda/vulkan/cpu)

    for line in sys.stdin:
        request = json.loads(line)        # une ligne = une requête JSON
        handle_request(request, manager, registry, db)
```

---

## 2. Émission d'événements

```python
[Phase 1]
def emit_event(event: str, payload: dict):
    # NOTE : sérialise UN objet JSON sur UNE ligne, sans retour à la ligne interne.
    out = json.dumps({"event": event, **payload}, ensure_ascii=False)
    sys.stdout.write(out + "\n")
    sys.stdout.flush()
```

---

## 3. Gestionnaire de modèles (`ModelManager`)

```python
[Phase 1]
class ModelManager:
    # NOTE : UN SEUL modèle résident en VRAM à la fois (borne 8 Go).
    # Le LLM principal reste "chaud" entre deux requêtes ; les autres sont lazy.

    def __init__(self):
        self.current_name = None          # nom du modèle actuellement chargé
        self.current_model = None         # handler du modèle chargé

    def use_model(self, name):
        # NOTE : context manager — `with manager.use_model("LFM2.5-8B") as llm:`
        # Si le bon modèle est déjà chargé ➜ réutilisation directe (pas de rechargement).
        # Sinon ➜ déchargement de l'ancien PUIS chargement du nouveau.
        if self.current_name == name:
            yield self.current_model
            return
        self._unload_current()            # libère la VRAM avant de charger
        self.current_model = self._load_model(name)
        self.current_name = name
        yield self.current_model
        # ⚠️ on ne décharge PAS automatiquement : le LLM reste chaud pour le tour suivant.

    def _load_model(self, name):
        # resolve_n_ctx : 128k si la VRAM le permet, sinon repli (ex. 32k).
        cfg = MODEL_CONFIGS[name]
        n_ctx = resolve_n_ctx(name, self.free_vram)
        kwargs = {"model_path": cfg.path, "n_ctx": n_ctx, "n_gpu_layers": cfg.n_gpu_layers}
        if cfg.supports_vision:           # ⚠️ vision : requiert mmproj + chat_handler, sinon l'image devient du texte
            kwargs["mmproj"] = cfg.mmproj
            kwargs["chat_handler"] = build_handler(cfg)
        return Llama(**kwargs)

    def _unload_current(self):
        # NOTE : libération EXPLICITE — close() + del + gc.collect() + attendre la libération VRAM.
        self.current_model.close()
        del self.current_model
        gc.collect()
```

> **Pourquoi tant de soin sur le déchargement ?** `llama.cpp` ne libère pas toujours la VRAM instantanément. Sans `close()` + `del` + attente, le modèle suivant risque un dépassement mémoire.

---

## 4. Détection matérielle

```python
[Phase 1]
# hardware.py — détecte le backend disponible (CUDA > Vulkan > CPU).
# NOTE : indispensable pour choisir n_gpu_layers et résoudre le contexte.
class HardwareDetector:
    def detect(self):
        return "cuda" if supports_cuda() else "vulkan" if supports_vulkan() else "cpu"
```

---

## 5. Configuration et chemins

```python
[Phase 1]
# defaults.py / paths.py — centralisation, PAS de valeurs en dur dans le code.
MODEL_CONFIGS = {
    "LFM2.5-8B":   {"path": "...", "n_gpu_layers": 24, "supports_vision": False},
    "LFM2.5-VL-3B": {"path": "...", "mmproj": "...", "supports_vision": True},
    "GLM-OCR":     {"path": "...", "mmproj": "...", "supports_vision": True},
}
# ⚠️ "supports_vision=False" sur le 8B : garde-fou pour ne jamais lui envoyer d'image.
```

---

## Correspondance

- [Annexe 02 — Quantification GGUF](../annexes/02_quantification_gguf.md)
- [Annexe 06 — Bonnes pratiques Python](../annexes/06_bonnes_pratiques_python.md)
- Suite : [backend_agent_loop.md](backend_agent_loop.md)