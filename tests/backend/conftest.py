"""Isole la suite de tests des données réelles de l'utilisateur.

Ce module est importé par pytest AVANT tout module de test, donc avant le
premier `import backend.*`. C'est la seule fenêtre où la redirection est
possible : `backend/db/database.py:21-24` fige `DB_PATH` à l'import, et
`resolve_data_dir` retombe sinon sur la racine XDG de l'utilisateur — celle de
l'application installée, pas celle du projet.

Sans cette isolation, `test_chaine_pieces_jointes.py` écrivait dans la base de
l'utilisateur à chaque exécution : treize conversations « test » par passage,
plusieurs centaines par heure de suite. Le projet ne s'en apercevait pas, parce
que l'application lancée par `npm start` lit `data/` (mode portable) et non la
racine XDG. Les deux bases coexistaient, et seule celle-ci se salissait — au
point d'avoir atteint 1136 conversations, toutes nommées « test ».

La base de la suite est TOUJOURS neuve. Copier celle de l'utilisateur, même
dans une racine temporaire, lasse à la suite des tests la lecture d'un état
amorcé par des données personnelles : le jour où cette base change, la suite
change de comportement sans qu'aucun test n'ait bougé. Un schéma créé à zéro
n'a pas cette propriété.
"""

import os
import shutil
import tempfile
from pathlib import Path


def _isoler() -> None:
    racine = Path(tempfile.mkdtemp(prefix="thot-tests-"))
    data = racine / "data"
    (data / "database").mkdir(parents=True, exist_ok=True)

    # `THOT_DB_PATH` prime sur toute resolution (database.py:21). Les deux autres
    # suffisent a rediriger blobs et tampon, qui suivent la racine de donnees.
    os.environ["THOT_DB_PATH"] = str(data / "database" / "thot-phase-1.db")
    os.environ["THOT_PORTABLE"] = "1"
    os.environ["THOT_APP_DATA_DIR"] = str(data)

    # Le schéma est créé ici, après la redirection : `init_db()` lit `DB_PATH`,
    # figé à l'import de `backend.db.database` avec la valeur posée ci-dessus.
    from backend.db.database import init_db

    init_db()


_isoler()


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    """Emporte la base temporaire : elle n'a rien à faire après la suite."""
    racine = os.environ.get("THOT_APP_DATA_DIR", "")
    if not racine:
        return
    shutil.rmtree(Path(racine).parent, ignore_errors=True)