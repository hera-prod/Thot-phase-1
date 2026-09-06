# Annexe 06 — Bonnes pratiques Python

Cette fiche couvre les bonnes pratiques Python transverses au projet : **IPC**, **sidecar/PyInstaller**, **concurrence**, **persistance**.

---

## 1. IPC JSON Lines (stdin/stdout)

- Un message = **une ligne JSON**. Ne jamais imprimer de log sur `stdout` (cela corrompt le protocole) : écrire les logs sur **stderr** ou dans un fichier.
- Purger les sorties non-JSON (print de debug, `warnings`).
- Sérialiser proprement (`json.dumps(ensure_ascii=False)`) et terminer chaque ligne par `\n`, puis `flush`.
- Distinguer **requêtes** (requests) et **événements** (événements poussés par le backend).

## 2. Sidecar et PyInstaller

- Le backend Python est packagé en **binaire autonome** (`--onefile`) géré comme *sidecar* par Tauri (spawn + pipe stdin/stdout).
- Utiliser `--collect-all backend` pour que PyInstaller collecte le package (évite `ModuleNotFoundError`).
- Éviter les dépendances natives coûteuses à packager ; privilégier des wheels précompilés (ex. `llama-cpp-python` CUDA).

## 3. Concurrence et threads

- La **boucle d'inférence** (orchestrateur → générateur) est séquentielle par conception.
- Les **périphériques** (micro, audio) vivent dans des **threads séparés** pour ne jamais bloquer l'inférence.
- Le **chargement/déchargement de modèles** est un point de contention : un seul gestionnaire (`ModelManager`) sérialise l'accès GPU.

## 4. Persistance (SQLAlchemy + SQLite)

- Définir les modèles dans un seul module ([pseudo-code — base de données](../pseudocode/backend_db.md)).
- Utiliser `CASCADE` et des **index** sur les colonnes de jointure fréquentes.
- Stocker les fichiers **sur disque** (chemin + hash), pas en BLOB dans la base.
- Prévoir des colonnes d'extension (`media_kind`, `visibility`, `utility_score`) dès le départ pour éviter les migrations.

## 5. Robustesse

- Gérer le **OOM** par repli (réduire `n_gpu_layers`, couper `flash_attn`, recharger sans mmproj).
- Libérer **explicitement** la VRAM (`close()` + `del` + `gc.collect()`) après usage d'un modèle secondaire.
- Journaliser les erreurs de manière structurée, sur stderr.

---

## Sources

- Documentation SQLAlchemy : https://docs.sqlalchemy.org/
- Documentation PyInstaller : https://pyinstaller.org/
- Documentation `llama-cpp-python` : https://llama-cpp-python.readthedocs.io/