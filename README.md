# Thot

Projet d’assistant conversationnel local, développé en cinq phases, avec un profil cible sous 8 Go de VRAM. La finalité inclut des distributions adaptées à Windows, Linux et macOS et un mode portable par dossier, notamment sur clé USB ; ces objectifs restent à qualifier sur les paquets réels.

## Point d’entrée

La [documentation centrale](documentation/README.md) décrit la préproduction : contrats, cinq phases, annexes sourcées, pseudo-code, portabilité et tests. Commencer par les [contrats transversaux](documentation/contrats_transversaux.md) avant de transformer les schémas en code.

- **[Audit de préproduction](documentation/audit_preproduction.md)** : corrections et écarts identifiés, sans modification du code applicatif.
- **[Portabilité](documentation/portabilite.md)** : exigences de dossier autonome, dépendances hôtes et essais par cible.
- **[Annexes](documentation/annexes/README.md)** : recherche et ressources officielles avec limites d’application.
- **[Pseudo-code](documentation/pseudocode/README.md)** : flux non exécutables, annotés par phase.
- **[Tests et qualification](documentation/tests_acceptation.md)** : scénarios attendus, non déclarés réussis sans preuve.

## État du dépôt

`backend/`, `frontend/` et `src-tauri/` contiennent un prototype Python, React/Vite et Tauri. La documentation décrit une cible plus rigoureuse, pas une certification de l’implémentation présente.

Aucune nouvelle fonctionnalité applicative ni correction de runtime n’est incluse dans cette révision documentaire. Les versions, commandes de build et prérequis seront figés dans un guide d’exécution après qualification ; l’ancien conseil d’installer les dépendances dans le Python système n’est plus présenté comme parcours utilisateur.
