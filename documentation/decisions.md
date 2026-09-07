# Décisions, hypothèses et limites de validation

Ce registre sépare exigences utilisateur, choix de préproduction et questions encore ouvertes. Une documentation cohérente ne constitue pas une preuve de bon fonctionnement ; chaque décision provisoire possède donc une méthode de validation.

## Exigences fixées

| ID | Exigence | Conséquence |
| :--- | :--- | :--- |
| E01 | Application locale, cible de profil sous 8 Go de VRAM | Modèles séquentiels, budget mesuré, pas de service LLM distant obligatoire |
| E02 | Grand contexte LFM2.5-8B-A1B déjà essayé par l’utilisateur | Conserver la cible, reproduire la configuration avant de certifier |
| E03 | Orchestrateur prépare, générateur rédige | Base de contexte commune ; aucun raisonnement d’orchestration envoyé au générateur |
| E04 | Développement en cinq phases cumulatives | Jalons testables, extensions sans promettre absence totale de migration |
| E05 | Application portable par dossier et clé USB, notamment Windows | Chemins relatifs, données gérées, paquets par cible, prérequis hôtes annoncés |
| E06 | L’utilisateur écrit le vrai code | Cette révision ne modifie que du Markdown et du pseudo-code non exécutable |
| E07 | Fichiers utilisateur sous contrôle utilisateur | IA en lecture seule ; propositions de modifications comme nouveaux artefacts |
| E08 | Réutiliser sources locales avant le réseau | Suffisance et fraîcheur contrôlées, mode local strict disponible |

## Choix de préproduction retenus

| ID | Choix | Pourquoi | Réversibilité |
| :--- | :--- | :--- | :--- |
| D01 | Conserver Tauri/React et sidecar Python, contrôle JSON Lines | Cohérence avec le prototype ; séparation UI/inférence | Le protocole versionné permet un autre transport plus tard |
| D02 | Workers d’inférence isolables avec ordonnanceur unique | Arrêt d’un calcul natif et cycle de vie explicites | Mécanisme exact à prototyper, contrat d’arrêt stable |
| D03 | Données avec IDs stables, blobs et rattachements logiques | Déduplication, portabilité, partage sans cascade destructrice | Migration depuis `Source` existant nécessaire |
| D04 | Historique original conservé, snapshots de contexte versionnés | Éviter perte irréversible à la compaction | Résumé régénérable |
| D05 | Métadonnées et droits minimaux en phase 1, index sémantique en phase 4 | Ne pas surcharger le premier jalon | Migrations explicites |
| D06 | Audio CPU comme profil initial ; dictée finale avant envoi | Éviter concurrence GPU et erreurs d’envoi implicite | Streaming ultérieur après mesures |
| D07 | Promotion mémoire validée par l’utilisateur initialement | Éviter accumulation autonome de conclusions erronées | Automatisation optionnelle et journalisée ultérieure |
| D08 | Windows portable autonome comme premier paquet à qualifier | Exigence concrète exprimée ; tester la chaîne complète tôt | Ne change pas les contrats multiplateformes |
| D09 | Runs de production sans conversation pour le studio, portée bibliothèque personnelle explicite | Ne pas polluer le chat ni inventer de message pour produire une image | Le rattachement ultérieur reste une commande autorisée |

Ces choix sont des recommandations de conception de la révision, pas des décisions déjà codées ou approuvées par un benchmark. Les ajuster par une décision documentée si un prototype révèle une contrainte réelle.

## Questions à clôturer par expérimentation

| ID | Question ouverte | État actuel | Essai/critère de clôture |
| :--- | :--- | :--- | :--- |
| O01 | Configuration LFM grand contexte sous 8 Go | Essai utilisateur rapporté, détails complets non joints ici | Checkpoint/checksum, runtime/version, contexte exact, cache, pic et latence sur machine cible |
| O02 | Support LFM-VL/GLM-OCR dans le binding choisi | Ne pas inférer celui du binding depuis celui de llama.cpp | Images de contrôle et PDF, template/projection corrects, pas de fallback d’architecture |
| O03 | Pipeline FLUX klein 9B quantifié jusqu’à 4 références | Essai utilisateur rapporté, pipeline à relever | Workflow/config exacts, encodeur, VAE, quantifications, dimensions, durée, mémoire et licence |
| O04 | Versions minimales Windows/Linux/macOS et architectures | Non fixées dans cette préproduction | Build distribué lancé sur chaque cible propre annoncée |
| O05 | Runtime web Windows embarqué ou hôte | Fixed Version à étudier pour édition autonome | Lancement hors ligne sans WebView préinstallé, profil/caches maîtrisés, redistribution vérifiée |
| O06 | Packaging et relance des workers | Dossier par cible proposé | Annuler/forcer l’arrêt/recharger depuis paquet final sans Python installé |
| O07 | Backend GPU par famille de modèle sur AMD/Apple | À qualifier séparément | Texte, vision, OCR et image testés individuellement puis swap entre eux |
| O08 | Budget CPU/RAM pour STT et TTS français | Pas de modèle audio figé arbitrairement | Corpus français, silence/bruit, machine CPU minimale, mémoire résidente et latence mesurées |
| O09 | Fournisseur de recherche et gestion de clé | Non choisi | Coûts/conditions/licence, confidentialité, quotas, limites du moteur, mode hors ligne |
| O10 | Mode SQLite et politique de sauvegarde sur clé | À choisir, pas de WAL imposé aveuglément | Crash/retrait simulé sur copie de test, restauration, verrouillage, support filesystem |
| O11 | Seuil de suffisance et ranking mémoire | Heuristiques proposées, non évaluées | Jeu de questions avec sources attendues, contradictions, obsolescence et hors périmètre |
| O12 | Rétention logs, caches, audio et sauvegardes | Principes fixés, durées paramétrables à définir | Budget disque et purge testés sans perdre les ressources référencées |
| O13 | Moteur PDF, parser et licences de distribution | Candidats dans annexes, choix final ouvert | Français/tableaux, pagination, licences des bibliothèques et fontes vérifiées |

Un point ouvert n’empêche pas d’écrire les contrats indépendants de sa réponse. Il empêche en revanche de publier une promesse matérielle, de compatibilité ou de redistribution qui dépend de lui.

## Livrables de qualification attendus

- **Manifeste matériel** : cible OS/architecture/pilote et ressources mesurées, sans extrapoler une mesure NVIDIA à Apple.
- **Manifeste des modèles** : versions, fichiers auxiliaires, checksums, licences et paramètres reproductibles.
- **Rapport par phase** : critères passés, échoués, non testés ; aucune coche implicite.
- **Rapport de distribution** : résultats depuis le paquet réellement transmis à l’utilisateur, pas depuis l’environnement de développement.

Le [contrat commun](contrats_transversaux.md), la [portabilité](portabilite.md) et les [tests](tests_acceptation.md) constituent les critères de ces livrables. Les [annexes](annexes/README.md) sont la base documentaire, non leur substitut.
