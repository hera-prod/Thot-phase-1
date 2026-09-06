# Phase 5 — Espaces de travail et mémoire

> **Objectif** : passer d'une collection de discussions **indépendantes** à des **espaces de travail** qui partagent des fichiers et une mémoire persistante, avec un référencement par score d'utilité.

---

## 1. Ce qui est nouveau

- **Espaces de travail** (`workspace`) : un conteneur logique qui regroupe plusieurs discussions.
- **Dossier utilisateur** (`workspace_user`) : fichiers déposés par l'utilisateur, partagés entre les discussions de l'espace.
- **Dossier mémoire** (`workspace_memory`) : synthèses générées par le système (résultats de recherche pertinents, fiches).
- **Nouveau niveau de recherche** : le **2.5** (exploration des fichiers de l'espace) entre les documents joints et le web.
- **Système de référencement** : plus une fiche a été utile, plus elle est prioritaire.

---

## 2. Architecture relationnelle

Une discussion n'est plus une entité isolée : elle **appartient** à un espace.

```
workspace
   ├── sessions (1..n discussions)
   ├── workspace_user     (fichiers de l'utilisateur — lecture seule pour l'IA)
   └── workspace_memory   (synthèses écrites par l'IA)
```

- Les fichiers ne sont **pas copiés** d'un dossier à l'autre : ils sont **référencés par chemin absolu** via une table de liaison.
- Supprimer une session ne détruit pas les documents partagés de l'espace.

référence : [Pseudo-code backend — espaces](../pseudocode/backend_espaces.md).

---

## 3. Cloisonnement sources / mémoire

Cette séparation est une règle de **confiance** essentielle :

| Dossier | Qui écrit ? | Droit de l'IA |
| :--- | :--- | :--- |
| `workspace_user` | l'utilisateur uniquement | **lecture seule** (explorer, citer) |
| `workspace_memory` | l'orchestrateur | lecture + écriture |

> L'IA ne doit **jamais** pouvoir altérer les documents originaux de l'utilisateur. Si l'utilisateur purge la mémoire, ses fichiers restent intacts.

---

## 4. Cascade de recherche actualisée (avec niveau 2.5)

| Niveau | Périmètre | Source |
| :--- | :--- | :--- |
| 1 | Contexte immédiat | mémoire active de la discussion |
| 2 | Pièces jointes directes | fichiers de la session en cours |
| **2.5a** | **Dossier utilisateur de l'espace** | fichiers partagés de l'espace |
| **2.5b** | **Mémoire de l'espace** | synthèses classées par score |
| 3 | Réseau ponctuel | recherche web simple |
| 4 | Réseau itératif | deep research |

---

## 5. Algorithme de référencement (score d'utilité)

Chaque fiche de la mémoire possède un **score** fondé sur trois signaux simples (en SQLite) :

1. **récence** d'accès ;
2. **fréquence** d'utilisation ;
3. **validation** lors d'une réponse antérieure.

Quand l'orchestrateur résout une requête grâce à une fiche, son compteur est incrémenté. Lors de l'exploration, le modèle ne reçoit que les **5 fiches au meilleur score** dans sa table des matières — ce qui garde le prompt léger tout en privilégiant les connaissances éprouvées.

référence : [Annexe 07 — Agents, recherche et mémoire](../annexes/07_recherche_agents_memoire.md).

---

## 6. Préparation du schéma de données

Pour éviter une refonte, le schéma SQLite défini en phase 1 doit déjà prévoir :

- table `workspaces` (id, nom, chemins des répertoires) ;
- table `sessions` avec colonne `workspace_id` ;
- table `artifacts` avec `visibility` (`session`, `workspace_user`, `workspace_memory`) et `utility_score` (initialisé à 0).

> **Règle d'écriture** : le gestionnaire de fichiers du backend refuse tout accès en écriture sur les chemins de `workspace_user`.

référence : [Pseudo-code backend — base de données](../pseudocode/backend_db.md).

---

## 7. Points de vigilance

- **`workspace_user` ≠ `workspace_memory`** : ne jamais laisser l'IA écrire dans le dossier utilisateur.
- **Score ≠ contenu** : le référencement classe les *fiches*, il ne modifie pas les documents.
- **Partage par référence**, pas par copie : éviter les doublons sur disque.
- **Le niveau 2.5 est local** : il ne déclenche aucun appel réseau.