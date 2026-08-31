# Wrapper pour PyInstaller — évite le problème `No module named 'backend'` quand on build backend/main.py directement
# Ce fichier est à la racine Thot-phase-1/, donc `backend` est un package top-level trouvable avec pathex=.
import backend.main  # noqa: F401 — force PyInstaller à collecter tout le package backend
from backend.main import *  # noqa: F401,F403
