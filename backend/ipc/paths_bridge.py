"""Résolution des chemins de données pour la couche IPC.

`electron/paths.cjs` fait le même travail pour le processus principal, et les
deux doivent tomber sur les MÊMES dossiers : sinon le renderer lirait un fichier
que le backend vient d'écrire ailleurs. Ce module ne réinvente donc rien — il
délègue à `backend.config.paths`, qui porte déjà le contrat portable — il rend
juste ces règles accessibles au module des pièces jointes.
"""

from __future__ import annotations

from pathlib import Path

from backend.config.paths import get_data_dir, get_temp_dir, is_portable_mode


def data_dirs() -> tuple[Path, Path]:
    """(data_dir, temp_dir) — crée l'arborescence attendue."""
    data = get_data_dir()
    for name in ("database", "blobs", "logs", "temp", "backups"):
        (data / name).mkdir(parents=True, exist_ok=True)
    temp = get_temp_dir()
    temp.mkdir(parents=True, exist_ok=True)
    return data, temp


def blobs_root() -> Path:
    cible = get_data_dir() / "blobs"
    cible.mkdir(parents=True, exist_ok=True)
    return cible.resolve()


def temp_root() -> Path:
    return get_temp_dir().resolve()


__all__ = ["data_dirs", "blobs_root", "temp_root", "is_portable_mode"]
