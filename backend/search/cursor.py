# backend/search/cursor.py — curseur de pagination lié à son classement
#
# D'OÙ VIENT CE FICHIER : le contrat l'impose — « SI révision du classement
# change entre deux pages / Invalider le curseur ou refaire la requête
# explicitement ; NE PAS MASKER DES RÉSULTATS PAR DÉCALAGE SILENCIEUX »
# (`backend_espaces.md:77-79`). C'est aussi ce que fait `fts.search` depuis le
# début : un curseur n'est pas un décalage, c'est un DÉCALÉGENDÉ par l'ordre qui
# l'a produit.
#
# POURQUOI UN MODULE : trois appelants ont des classements mutables (les
# documents n'ont pas d'usage, mais la mémoire a `usage_count` et `last_used_at`
# qui bougent entre deux pages). Un décalage nu=y perd une fiche et n'en duplique
# aucune — le pire mode de panne, invisible. Le même encodage pour tous évite
# qu'un appelant qui traverse deux services ait deux grammaires.
#
# NOTE DE PÉRIMÈPTRE : `fts.py` conserve ses deux fonctions privées, identiques,
# pour ne pas faire vibrer le moteur de recherche. Elles servent
# `search_documents` depuis des versions antérieures à ce lot. Le jour où fts.py
# est modifié, il doit venir chercher ici.
from __future__ import annotations

import base64
import hashlib
import json
from typing import Any


def encode(fingerprint: str, offset: int) -> str:
    raw = json.dumps({"qh": fingerprint, "off": offset}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode()


def decode(cursor: str) -> dict[str, Any] | None:
    """None si le curseur est illisible — jamais d'exception, comme `fts.py`."""
    try:
        return dict(json.loads(base64.urlsafe_b64decode(cursor.encode()).decode()))
    except Exception:
        return None


def fingerprint(*parts: Any) -> str:
    """Empreinte 16 hex. On hache l'ORDRE RÉSULTANT quand l'ordre est mutable,
    pas seulement la requête : deux requêtes identiques ne produisent pas le
    même classement si un usage a bougé entre les deux pages."""
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
