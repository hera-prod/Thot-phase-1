# Thot : documentation centrale

Thot est un projet d’assistant local développé par phases, avec une cible de profil sous 8 Go de VRAM et une finalité multiplateforme et portable par dossier. Cette documentation est le contrat de préproduction ; elle ne prétend ni que les cinq phases sont implémentées ni que toutes les plateformes ont été testées.

Révision d’audit du 6 septembre 2026, fondée sur les exigences utilisateur et la relecture du prototype au commit `9646e60`. Le code applicatif est laissé à l’utilisateur ; seuls la documentation et les schémas de pseudo-code sont révisés.

## Lire dans cet ordre

| Document | Rôle | Quand le consulter |
| :--- | :--- | :--- |
| [Contrats transversaux](contrats_transversaux.md) | Vocabulaire, contextes, permissions, données, ressources et erreurs | Avant toute implémentation ; arbitre les ambiguïtés |
| [Portabilité et distribution](portabilite.md) | Dossier déplaçable, dépendances hôtes, données, sauvegardes et paquets par cible | Dès la phase 1, pas en fin de projet |
| [Décisions et hypothèses](decisions.md) | Exigences fixées, choix proposés, points à tester | Avant de figer une dépendance ou une promesse |
| [Documents de phase](#les-cinq-phases) | Parcours, périmètre, jalons et critères de livraison | Dans l’ordre de production |
| [Pseudo-code annoté](pseudocode/README.md) | Traduction non exécutable des contrats en flux frontend/backend | Avant d’écrire le code de chaque capacité |
| [Sommaire des annexes](annexes/README.md) | Ressources scientifiques et pratiques sourcées | Pour justifier ou réexaminer les choix techniques |
| [Tests d’acceptation](tests_acceptation.md) | Scénarios, résultats attendus et preuves de qualification | À chaque jalon et chaque paquet distribué |
| [Audit de préproduction](audit_preproduction.md) | Erreurs corrigées et écarts restant dans le prototype | Pour savoir ce qui a changé et ce qui n’est pas encore validé |

## Les cinq phases

| Phase | Capacité livrable | Précondition |
| :--- | :--- | :--- |
| [Phase 1 : socle et vision](phase_1_socle_et_vision.md) | Accueil, chat, historique, orchestrateur/générateur, pièces jointes, vision/OCR, compaction et premier profil portable | Contrats de contexte, stockage, IPC et runtime validés |
| [Phase 2 : génération d’images](phase_2_generation_image.md) | Studio, outil image, références et galerie | Ordonnanceur et artefacts stables |
| [Phase 3 : documents et voix](phase_3_documents_et_voix.md) | TXT/MD/CSV/PDF, galerie générale, export, dictée et lecture vocale | Publication atomique observable, audio indépendant |
| [Phase 4 : recherche](phase_4_moteur_recherche.md) | Fiches, recherche locale, cache web, recherche simple/approfondie, preuves | Portées d’accès et provenance établies |
| [Phase 5 : espaces et mémoire](phase_5_espaces_et_memoire.md) | Partage de fichiers, mémoire révisable, classement contextualisé | Identifiants et rattachements non destructeurs |

Une phase prépare la suivante par des interfaces stables, pas en codant toutes les fonctions futures. Les migrations sont normales et planifiées ; « aucune refonte ne sera jamais nécessaire » n’est pas un engagement réaliste.

## Architecture en une vue

```text
Interface React : saisie, navigation, états et aperçu des ressources
        |
Coquille Tauri/Rust : lancement, permissions locales et pont de contrôle
        |
Coordinateur Python : requêtes, ordonnanceur, contexte, droits, stockage
        |
        +-- Rôle orchestrateur : décide parmi les actions permises
        +-- Exécuteur logiciel : valide, exécute, conserve les observations
        +-- Rôle générateur : rédige, sans outils ni raisonnement d’orchestration
        +-- Compacteur : crée une projection, sans effacer les messages
        |
Workers/runtimes : texte, vision/OCR, image ; audio CPU en phase 3
        |
Racine de données gérée : base, blobs, preuves, snapshots et logs bornés
```

Le modèle texte peut être identique entre orchestrateur, générateur et compacteur. Les rôles et permissions restent distincts ; une location exclusive protège le profil GPU tandis que le lecteur de commandes et l’interface continuent à répondre.

## Les sous-entendus désormais explicites

- **Préparer n’est pas transmettre sa pensée** : le générateur reçoit la même base de dialogue, un brief structuré et les preuves retenues, jamais le raisonnement privé de l’orchestrateur ; voir [C03 à C05](contrats_transversaux.md).
- **Compacter n’est pas supprimer** : les messages restent archivés et la projection compacte est versionnée ; voir [C06](contrats_transversaux.md).
- **Local n’est pas sans permissions** : les outils utilisent des IDs autorisés, pas des chemins inventés par le modèle ; voir [C10 à C12](contrats_transversaux.md).
- **Cache n’est pas vérité éternelle** : réemploi selon fraîcheur, sources conservées et revalidation possible ; voir [C15 à C17](contrats_transversaux.md).
- **Un dossier portable n’est pas un binaire universel** : données autonomes et paquets adaptés à chaque cible, avec dépendances et limites annoncées ; voir [portabilité](portabilite.md).
- **Bonne documentation n’est pas application certifiée** : chaque promesse se clôt par un test, les points non mesurés restant au [registre](decisions.md).

## Références structurantes

La [fiche LFM](annexes/01_lfm_et_architecture.md) distingue le modèle réel des suppositions sur son contexte ; la [fiche runtimes/GGUF](annexes/02_quantification_gguf.md) précise les contraintes de compatibilité. La [fiche FLUX](annexes/03_flux_klein_generation.md) et la [fiche audio](annexes/04_audio_embarque.md) séparent capacités amont et profils matériels à mesurer.

Les [outils de documents](annexes/05_generation_documents_python.md), les [bonnes pratiques Python](annexes/06_bonnes_pratiques_python.md), les [papiers agents/recherche/mémoire](annexes/07_recherche_agents_memoire.md) et les [références de distribution](annexes/08_portabilite_et_distribution.md) fournissent des sources consultables. Chaque annexe explique ce que ces sources justifient, et ce qu’elles ne démontrent pas pour Thot.

## Relation avec le dépôt existant

Le prototype comprend `backend/`, `frontend/` et `src-tauri/`, avec des parties déjà développées. Ne pas assimiler présence de fichiers, commentaires « garanti » ou anciens résultats de tests à la validation de la cible décrite ici.

Les changements nécessaires au code sont identifiés dans l’[audit](audit_preproduction.md), sans être appliqués dans cette révision. Pour revenir au dépôt, utiliser le [README principal](../README.md).
