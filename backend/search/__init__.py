# backend/search/__init__.py — Moteur de recherche documentaire locale (annexe 11, Lot A)
#
# D'OÙ VIENT CE PAQUET : l'annexe 11 impose un service déterministe et permissionné
# (FTS5 + fiches versionnées + scope-check) dont le LLM n'est qu'un utilisateur guidé.
# OÙ VA QUOI : scope.py (droits AVANT retrieval) → fts.py (lexical + ranking) →
# cards.py (fiches N+1) + observations.py (résultats immuables). Exécuteurs dans
# loop.py (_execute_search_documents) et outil structuré tools/search_documents.py.
# POURQUOI SÉPARÉ DE tools/ : le moteur est un service de stockage, pas un outil ;
# les outils n'en sont que la façade validée par Pydantic.
from __future__ import annotations
