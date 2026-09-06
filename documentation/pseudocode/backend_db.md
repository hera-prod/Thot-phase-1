# Pseudo-code — Base de données et stockage

> Phases 1 → 5. Ce fichier couvre le **schéma SQLAlchemy**, le **stockage des fichiers** et son **évolution par phase**.

---

## 1. Principe global

- Base **SQLite** via **SQLAlchemy**.
- Les **fichiers** (images, PDF, CSVs) sont stockés **sur disque** dans un répertoire centralisé ; la base ne garde que le **chemin + hash + métadonnées**.
- ⚠️ **Jamais de base64 ni de BLOB** dans la base : un fichier est référencé, pas dupliqué.

---

## 2. Schéma phase 1

```python
[Phase 1]
class Conversation(Base):
    id, title, mode, created_at, updated_at
    messages = relationship("Message", cascade="all, delete-orphan")

class Message(Base):
    id, conv_id(FK), role, content, thinking, status, tokens_in, tokens_out, created_at
    # NOTE : "thinking" est stocké pour audit, mais JAMAIS réinjecté dans l'historique visible.

class ToolCall(Base):        # ⚠️ outil appelé par l'ORCHESTRATEUR, pas une réponse
    id, msg_id(FK), tool, args(JSON), result, ok, latency_ms

class Source(Base):          # fichier déposé / généré
    id, msg_id(FK), media_kind, original_name, mime_type, sha256, relative_path, size_bytes
    # NOTE : "relative_path" = chemin relatif sous le répertoire média central.
```

---

## 3. Stockage des fichiers

```python
[Phase 1]
# utils/files.py — le backend copie le fichier déposé dans media/{sha256}.ext,
# puis enregistre une ligne "Source". Le hachage évite les doublons.
def ingest_file(original_path):
    digest = sha256(original_path)
    stored = media_root / f"{digest}{suffix}"
    if not stored.exists():
        copy(original_path, stored)
    return {"sha256": digest, "relative_path": stored.relative_to(media_root)}
```

---

## 4. Évolution du schéma par phase

### [Phase 2] La galerie

```python
[Phase 2]
# NOTE : réutilise la table Source. Ajout d'une colonne de CATÉGORIE pour filtrer.
# ⚠️ dès phase 2, prévoir la catégorie (image/table/document/raw) même si on n'a que des images.
class Source(Base):
    category = "image" | "table" | "document" | "raw"   # ➜ anticipe la galerie unifiée phase 3
```

### [Phase 3] Artefacts générés

```python
[Phase 3]
# NOTE : fichiers créés par le backend (CSV/PDF/Markdown) → réutilisent la même table.
class Artifact:  # fusion possible avec Source ; notion d'artifact = fichier produit
    id, session_id(FK), file_type, file_path, created_at
    # ⚠️ l'artifact rejoint la galerie avec sa catégorie (table/document/image).
```

### [Phase 5] Espaces et mémoire

```python
[Phase 5]
class Workspace(Base):
    id, name, user_dir, memory_dir        # chemins des répertoires de l'espace
    sessions = relationship("Session")

class Session(Base):                        # une discussion APPARTIENT à un espace
    id, workspace_id(FK), ...

class Artifact(Base):                        # enrichi :
    visibility = "session" | "workspace_user" | "workspace_memory"
    utility_score = 0                        # pour le référencement (voir backend_espaces)
```

> **Règle d'écriture** : le gestionnaire de fichiers **refuse** toute écriture sur les chemins `workspace_user` (sanctuaire utilisateur). Seule `workspace_memory` est écrite par l'orchestrateur.

---

## Points de vigilance

- Ne pas **copier** les fichiers entre discussions/espace : référencer par chemin.
- Supprimer une session ne doit pas détruire les fichiers partagés de l'espace (pas de suppression en cascade des `Source` partagés).
- Toujours ajouter des **index** sur les colonnes de jointure (`conv_id`, `sha256`).

---

## Correspondance

- [Document — Phase 5](../phase_5_espaces_et_memoire.md)
- Annexes : [06 — Bonnes pratiques Python](../annexes/06_bonnes_pratiques_python.md)
- Avant : [backend_outils.md](backend_outils.md)