"""Erreurs partagées de l'ingestion — sans dépendance, pour éviter les cycles.

`BailPerdu` est levée quand une écriture conditionnée à un jeton de claim
constate que le claim n'appartient plus à l'appelant. Elle doit traverser les
couches sans être avalée : un worker expiré qui continue d'écrire après la
reprise est précisément le défaut que le fencing supprime.

Ce module ne dépend d'aucun autre module d'ingestion : `etat`, `bail`,
`observations` et `attachment_ingest` peuvent tous l'importer sans créer de
cycle.
"""

from __future__ import annotations


class BailPerdu(RuntimeError):
    """Le claim d'ingestion a été perdu (expiré ou volé par un autre propriétaire).

    `artifact_id` et `token` sont portés pour que le journal nomme l'artefact et
    le jeton concernés : une perte de bail silencieuse rendrait la double
    écriture indétectable.
    """

    def __init__(self, artifact_id: str, token: str | None = None) -> None:
        self.artifact_id = str(artifact_id or "")
        self.token = token
        super().__init__(f"BAIL_PERDU:{self.artifact_id}")


__all__ = ["BailPerdu"]
