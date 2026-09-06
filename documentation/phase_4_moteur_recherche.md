# Phase 4 — Moteur de recherche

> **Objectif** : donner à l'orchestrateur la capacité de **rechercher des informations sur Internet**, avec une recherche simple et un **deep research**, tout en limitant les requêtes réseau par une cascade locale prioritaire.

---

## 1. Ce qui est nouveau

- **Recherche web simple** : une requête synthétique → faits ponctuels avec citations.
- **Deep research** : boucle itérative (sous-questions, exploration de liens, rapport de synthèse).
- **Règle de limitation** : ne pas chercher sur Internet si l'information est déjà disponible **localement** (documents joints, contexte).
- **Fiches de fichiers** : chaque fichier reçoit une description + une recommandation d'utilisation.
- **Cache de pages web** : les pages consultées sont archivées pour ne jamais refaire deux fois la même recherche.

---

## 2. La cascade de recherche (local avant distant)

L'orchestrateur évalue la **source la plus proche** avant d'élargir. L'ordre est strictement croissant en coût :

1. **Niveau 1 — Session** : le contexte immédiat suffit, pas d'appel d'outil.
2. **Niveau 2 — Documents joints** : chercher dans les fichiers de la discussion en cours.
3. **Niveau 3 — Recherche web simple** : un appel ponctuel à un moteur.
4. **Niveau 4 — Deep research** : investigation autonome multi-rebonds.

> **Principe** : si l'information cherchée est dans un PDF joint, on la prend dans le PDF et on **n'émet aucune requête réseau**.

référence : [Annexe 07 — Agents, recherche et mémoire](../annexes/07_recherche_agents_memoire.md) et [Pseudo-code backend — recherche](../pseudocode/backend_recherche.md).

---

## 3. Fiches descriptives des documents

Pour savoir **sans relire tout le fichier** s'il faut l'inspecter, chaque document reçoit une fiche compacte, injectée dans le prompt de l'orchestrateur :

- titre ;
- résumé (2 phrases) ;
- entités / mots-clés ;
- recommandation d'usage.

> C'est la « table des matières » qui évite au modèle d'ouvrir inutilement chaque PDF.

---

## 4. Cache de pages web (fichiers fantômes)

Les pages consultées sont **nettoyées** (extraction du texte via `trafilatura` / `BeautifulSoup`) puis archivées en base avec :

- l'URL source ;
- une empreinte (hash) ;
- le texte nettoyé.

Quand une question porte sur un sujet déjà exploré, l'orchestrateur consulte **d'abord ce cache local** plutôt qu'un nouvel appel réseau. Ce sont des « fichiers fantômes » : invisibles pour l'utilisateur, réutilisables par le modèle.

---

## 5. Points de vigilance

- **Local avant distant** : toujours tester les niveaux 1–2 avant d'activer le réseau.
- **Ne jamais faire deux fois la même recherche** : vérifier le cache avant d'émettre une requête.
- **Deep research = coût élevé** : réservé aux demandes explicites d'analyse complète.
- **Citations** : la recherche simple doit citer ses sources (URL) dans la réponse.