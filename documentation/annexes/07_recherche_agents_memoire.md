# Annexe 07 — Agents, recherche et mémoire

Cette fiche documente les principes d'**agents**, de **recherche en cascade** et de **mémoire** qui sous-tendent les phases **4** (recherche) et **5** (espaces).

---

## 1. Architecture agentique (orchestrateur / générateur)

Découpler la **planification** (orchestrateur, qui peut appeler des outils) de la **rédaction** (générateur, sans outil) est un patron courant dans les agents « planificateur-exécutant ». Avantages :
- un **historique propre** (pas de traces d'outils dans le dialogue visible) ;
- un contrôle clair de ce qui consomme le contexte ;
- une limitation naturelle des boucles d'outils.

## 2. Récupération en cascade (RAG hiérarchique)

Au lieu d'appeler systématiquement le web, on explore les sources par **coût croissant** :
1. contexte de la session ;
2. documents joints ;
3. fichiers de l'espace (user puis mémoire) ;
4. recherche web simple ;
5. deep research.

C'est une forme de **RAG** (Retrieval-Augmented Generation) **hiérarchisée**, où chaque niveau n'est activé que si le précédent est insuffisant (voir [phase 4](../phase_4_moteur_recherche.md)).

## 3. Mise en cache des recherches

Les pages web consultées sont nettoyées puis **archivées localement** (URL + empreinte + texte). Ce « cache fantôme » évite de ré-interroger le réseau pour un sujet déjà traité : un principe de **mémoire de session/externe** appliqué aux agents.

## 4. Mémoire d'espace et référencement

La mémoire d'un espace rassemble les **synthèses pertinentes** produites par l'agent. Chaque fiche porte un **score d'utilité** (récence + fréquence + validation), ce qui permet de ne présenter au modèle que les fiches les plus utiles.

C'est une forme de **mémoire externe persistante** avec **priorisation**, évitant d'envoyer tout l'historique dans le prompt.

## 5. Points clés

- **Local avant réseau** : économise le temps et les requêtes.
- **Cloisonner** les documents utilisateur (lecture seule) de la mémoire générée (écrite par l'IA).
- **Limiter le prompt** aux fiches les plus pertinentes (top-k par score).

---

## Sources

- LFM2 Technical Report (aperçu des architectures d'agents et de génération) : https://arxiv.org/html/2511.23404v1
- Documentation de récupération / RAG — module `trafilatura` (extraction web) : https://github.com/adbar/trafilatura