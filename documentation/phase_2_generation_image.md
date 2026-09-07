# Phase 2 — Génération et édition d’images

## Statut et résultat attendu

Cette phase décrit l’intégration cible d’un moteur d’images local, non une fonctionnalité déjà certifiée dans Thot.
Elle suit les [contrats C01–C19](contrats_transversaux.md), en particulier C07 pour les ressources et C13 pour le service de production.
L’utilisateur doit pouvoir créer une image dans un studio direct ou depuis une conversation, puis la retrouver et la réutiliser sans duplication logique.
L’essai utilisateur rapporté sur un profil 8 Go avec jusqu’à quatre références constitue un acquis à reproduire, pas une certification de tous les backends ([C19](contrats_transversaux.md#c19--distribution-qualification-et-décisions-ouvertes)).

## Objectifs et hors périmètre

- Ajouter un studio de génération avec prompt, paramètres exposés par le profil, références ordonnées, progression et annulation.
- Exposer `generate_image` au chat, sans donner au générateur de texte un accès aux outils.
- Prendre en charge création sans référence et édition guidée par une à quatre images pour le profil utilisateur à qualifier.
- Publier une image vérifiée, ses réglages et sa provenance dans le registre d’artefacts commun.
- Offrir une galerie d’images générées, des imports visibles sur filtre explicite et un rattachement volontaire à une conversation.

Hors périmètre : vidéo, entraînement, nombre illimité de références, collaboration distante et retouche destructive des originaux.
La galerie générale de documents et leurs formats arrivent en [phase 3](phase_3_documents_et_voix.md), sans seconde base parallèle ([C02](contrats_transversaux.md#c02--activation-par-phase)).
Une qualité artistique, une fidélité parfaite aux références ou une reproduction pixel à pixel entre backends ne sont pas garanties.

## Prérequis et décisions d’entrée

1. Vérifier les sorties de phase 1 : autorisations, artefacts, journal, ordonnanceur, annulation, publication atomique logique et packaging.
2. Rassembler les paramètres exacts de l’essai utilisateur : machine, OS, runtime, composants, quantifications, dimensions et nombre de références.
3. Vérifier la licence du checkpoint **FLUX.2 [klein] 9B** retenu, ses conditions d’usage et de redistribution dans l’[annexe 03](annexes/03_flux_klein_generation.md).
4. Inventorier chaque composant et son format réellement accepté par le backend ; un poids quantifié ne prouve pas la compatibilité du pipeline.
5. Constituer un corpus versionné : prompts simples, quatre références distinctes, images trop grandes, fichier corrompu et droits insuffisants.
6. Préparer les tests du studio autonome : `Run.kind=production`, IDs de conversation/message nuls et portée explicite de bibliothèque personnelle selon [C08](contrats_transversaux.md#c08--ipc-et-état-de-linterface) et [C10](contrats_transversaux.md#c10--stockage-identifiants-et-droits).

Ne pas transférer à la variante 9B les conditions de licence d’une autre variante de la famille ; l’autorisation de distribuer les poids est une vérification distincte du téléchargement technique ([annexe 03](annexes/03_flux_klein_generation.md)).
Le studio ne crée ni conversation ni message artificiel : son run de production admet `conversation_id=null` et `user_message_id=null`.
Le champ `conversation_id` reste présent dans les événements, avec valeur `null` ; `run_id` et `seq` assurent la corrélation et la reprise durable selon [C08](contrats_transversaux.md#c08--ipc-et-état-de-linterface).

## Pipeline exact à qualifier

| Élément | Information obligatoire du profil | Limite à ne pas masquer |
| :--- | :--- | :--- |
| Checkpoint de diffusion | Variante 9B exacte, révision, checksum, format et quantification | Le nom de famille seul ne suffit pas |
| Encodeur texte | Identifiant exact, révision, tokenizer et précision compatibles avec ce checkpoint | Ne pas présumer qu’il s’agit de CLIP |
| VAE | Identifiant compatible, précision, stratégie d’encodage/décodage | Sa quantification n’est pas validée par cette phase |
| Runtime | Version, backend matériel, opérateurs et formats disponibles | Un support amont ne qualifie pas l’application |
| Références | Ordre, dimensions, normalisation et transformations | Quatre entrées restent un profil à reproduire |
| Paramètres | Résolution, étapes, seed si disponible, guidage et réglages exposés | Ne pas généraliser les réglages d’un seul essai |

Les identifiants de l’encodeur et du VAE doivent être vérifiés contre la configuration du checkpoint et consignés avec les réserves de l’[annexe 03](annexes/03_flux_klein_generation.md).
L’hypothèse « backbone, VAE et CLIP sont tous quantifiés » n’est pas un contrat accepté ; mesurer chaque composant et valider les associations ([annexe 02](annexes/02_quantification_gguf.md)).
Le pipeline image est une **famille exclusive**, pouvant charger plusieurs composants nécessaires : ce n’est ni un modèle unique ni forcément un seul fichier.
Sur le profil 8 Go, elle ne partage pas sa location GPU avec texte ou vision ; son propre ordonnancement interne doit respecter la mémoire mesurée ([C07](contrats_transversaux.md#c07--ressources-runtimes-et-concurrence)).

## Responsabilités, entrées et sorties

| Responsable | Entrées | Sorties et interdictions |
| :--- | :--- | :--- |
| Studio direct | Prompt utilisateur, paramètres du profil, références choisies | Demande au service image, sans passage par l’orchestrateur |
| Orchestrateur du chat | Demande utilisateur et catalogue autorisé | Appel `generate_image` structuré, sans chemin arbitraire |
| Service image commun | Prompt borné, profil, IDs et clé idempotente | Validation, travail ordonnancé, résultat d’outil ou erreur |
| Ordonnanceur | Demande de famille image, état des workers | Location exclusive et libération confirmée |
| Worker image | Composants vérifiés et copies d’entrées autorisées | Fichier en préparation, étapes et diagnostics bornés |
| Service d’artefacts | Sortie vérifiée, réglages, références et portée | Blob, artefact, lien conversation ou bibliothèque personnelle, puis événement après commit |
| Générateur du chat | Même contexte partagé, brief et IDs de sortie | Présentation du résultat, sans nouvel appel d’image |
| Galerie | Vue autorisée des artefacts | Aperçu, détail, sélection et commandes utilisateur |

Studio et chat passent par le **même service**, le même registre d’artefacts et le même ordonnanceur ; seul le chat utilise l’orchestrateur ([C13](contrats_transversaux.md#c13--outils-de-production-de-contenu)).
L’outil renvoie identifiants, dimensions, type, statut et aperçu borné, pas un chemin proposé par le modèle ni des octets base64 dans le bus ([C08](contrats_transversaux.md#c08--ipc-et-état-de-linterface), [C10](contrats_transversaux.md#c10--stockage-identifiants-et-droits)).

## Parcours concrets : succès et échec

### Génération directe dans le studio

1. L’utilisateur choisit un profil disponible, renseigne son prompt et voit les limites de taille et de références.
2. Le backend valide la demande et crée un run de production sans conversation dans une file bornée ; un autre calcul lourd reste prioritaire jusqu’à libération.
3. Le service acquiert la famille image, charge ses composants et affiche les étapes réellement observables.
4. Le fichier est décodé et contrôlé avant publication dans la bibliothèque personnelle ; la galerie affiche un seul artefact avec ses paramètres.
5. L’utilisateur peut ensuite rattacher cet artefact à une conversation sans recopier ses octets ([C10–C13](contrats_transversaux.md#c10--stockage-identifiants-et-droits)).

Si le modèle manque, si le format est incompatible ou si la mémoire est insuffisante, afficher des erreurs distinctes.
Ne pas télécharger silencieusement un autre checkpoint, réduire les références sans accord, ni annoncer une image disponible avant publication.

### Édition depuis une conversation

1. L’utilisateur joint quatre images et décrit le rôle attendu de chacune ; le catalogue permet de les distinguer.
2. L’orchestrateur transmet leurs IDs dans un ordre explicite, le prompt et les seuls paramètres autorisés.
3. L’exécuteur revalide chaque rattachement ; les images d’origine restent immuables et les transformations sont enregistrées.
4. Après production, conserver les preuves et le résultat corrélé ; réacquérir le runtime texte avant toute nouvelle décision.
5. Le générateur présente l’artefact et ses limites depuis le contexte partagé, sans prétendre avoir validé visuellement tous les détails ([C03–C04](contrats_transversaux.md#c03--identité-du-contexte-et-séparation-des-rôles), [C13](contrats_transversaux.md#c13--outils-de-production-de-contenu)).

Une référence absente, interdite ou ambiguë doit produire une erreur ou une clarification ; jamais une substitution silencieuse.
Une cinquième référence sur le profil limité à quatre est refusée avant allocation, avec proposition de réduire la sélection.

### Annulation, panne et reprise

1. Annuler pendant encodage, échantillonnage ou décodage doit parvenir au coordinateur même si la bibliothèque native travaille.
2. Attendre l’arrêt effectif ou terminer le worker au délai configuré avant d’autoriser un autre chargement.
3. Une sortie non validée reste temporaire ; une sortie déjà publiée n’est pas supprimée par le nettoyage.
4. Après crash, réconcilier la clé idempotente et les artefacts enregistrés avant toute relance proposée à l’utilisateur ([C09–C11](contrats_transversaux.md#c09--annulation-reprise-et-idempotence)).

## Données, galerie et progression

Chaque artefact conserve checkpoint et versions, quantifications, réglages, seed si disponible, dimensions, références d’entrée et transformations.
Conserver aussi origine studio/chat, statut, run et clé d’effet idempotente ; les paramètres sensibles ne doivent pas devenir des logs publics ([C09](contrats_transversaux.md#c09--annulation-reprise-et-idempotence), [C13](contrats_transversaux.md#c13--outils-de-production-de-contenu)).
La galerie est une projection du registre `Artifact`, non une copie des anciennes sources dans une nouvelle table.
La portée bibliothèque personnelle ne donne aucun accès implicite aux conversations ; une sélection utilisateur crée un rattachement autorisé, après contrôle backend ([C10](contrats_transversaux.md#c10--stockage-identifiants-et-droits), [C13](contrats_transversaux.md#c13--outils-de-production-de-contenu)).
Afficher les générations par défaut, les imports sur filtre explicite, et exclure extractions et caches internes de la vue courante ([C10](contrats_transversaux.md#c10--stockage-identifiants-et-droits)).
Les catégories doivent être extensibles par migration pour la phase 3 ; ne pas développer ici les outils documentaires.
Une miniature absente n’invalide pas forcément l’original ; distinguer l’état d’aperçu de la disponibilité de l’artefact.
La progression utilise `run.stage` et les événements C08 : file, chargement, encodage, génération, décodage, publication.
Un pourcentage n’est affiché que si le runtime fournit une base réelle ; sinon montrer une étape indéterminée sans durée inventée ([C08](contrats_transversaux.md#c08--ipc-et-état-de-linterface)).
La suppression d’une vue ou d’une conversation ne détruit pas un blob encore référencé ; confirmer les conséquences avant purge ([C11](contrats_transversaux.md#c11--publication-de-fichiers-et-suppression)).

## Sécurité et limites

Valider le type réel, les dimensions décodées, le poids et le nombre de références avant chargement ; borner les images décompressées et les temporaires.
Les noms et chemins résolus restent sous contrôle backend ; une référence connue par hash ne donne pas accès à une autre conversation.
Le texte d’une image ou ses métadonnées ne peut ni modifier les droits ni déclencher un autre outil ([C18](contrats_transversaux.md#c18--sécurité-confidentialité-et-accessibilité)).
Préciser que l’édition peut altérer des détails, du texte ou des identités ; l’utilisateur doit pouvoir ouvrir l’image complète avant réutilisation.
Requalifier CPU, AMD, Apple et NVIDIA séparément, avec RAM, mémoire unifiée ou VRAM selon le matériel ; ne pas annoncer un repli fonctionnel non testé ([C07](contrats_transversaux.md#c07--ressources-runtimes-et-concurrence), [C19](contrats_transversaux.md#c19--distribution-qualification-et-décisions-ouvertes)).
Le packaging inclut les dépendances autorisées et leurs notices ; ne pas intégrer de poids dont la redistribution reste indécise ([portabilité](portabilite.md), [annexe 03](annexes/03_flux_klein_generation.md)).

## Exigences → conséquences → tests

Les critères sont proposés pour la qualification ; aucune mesure de réussite ou de performance n’est revendiquée ici.

| Exigence | Conséquence technique ou produit | Test d’acceptation proposé |
| :--- | :--- | :--- |
| Studio et chat unifiés — C13 | Une validation et une publication communes | Même demande par les deux entrées : mêmes règles, métadonnées requises et refus |
| Studio autonome et cloisonné — C08/C10 | Run de production sans conversation, lien bibliothèque explicite | Sur 10 productions : zéro message artificiel ; événements corrélés malgré `conversation_id=null` ; accès chat refusé avant rattachement |
| Pipeline exclusif — C07 | Location par famille, pas par fichier | Sur 10 alternances texte/image : zéro chevauchement interdit et aucun vieux handle utilisé |
| Composants exacts — C19 | Manifeste complet et compatibilité vérifiée | Refuser chaque cas de test avec encodeur, VAE, empreinte ou format incompatible |
| Profil quatre références — C02/C19 | Limite déclarée, transformations traçables | Reproduire l’essai 0/1/4 références ; refuser 5 ; relever mémoire et fidélité séparément |
| Droits des entrées — C10/C18 | Vérification avant décodage et après résolution | Zéro lecture sur toutes les références interdites ou chemins de contournement |
| Publication fiable — C11 | Contrôle puis commit, événements après commit | Couper à chaque étape de publication : aucun artefact annoncé sans fichier disponible |
| Annulation et idempotence — C09 | Arrêt réel et clé d’effet unique | Sur 10 annulations/relances : aucune double image logique ni destruction de sortie publiée |
| Portabilité — C19 | Profil et dépendances distribuables documentés | Déplacer la racine ; ouvrir toutes les images et métadonnées sans les anciens chemins |

## Jalons ordonnés et critères de sortie

| Ordre | Livraison attendue | Critère de passage à mesurer |
| :--- | :--- | :--- |
| P2-J1 | Licences, composants et contrat studio préparés | Aucun composant non identifié ; distribution documentée ; cas de test production/null/bibliothèque conformes à C08/C10/C13 |
| P2-J2 | Adaptateur image et ordonnanceur | Génération témoin ouvrable ; 10 alternances conformes à C07, nettoyage compris |
| P2-J3 | Studio, progression et annulation | Parcours direct au clavier sans conversation artificielle ; tests de corrélation/portée réussis ; arrêts aux trois étapes de calcul |
| P2-J4 | Outil chat et édition multi-références | Cas 0/1/4 traités ou limitations explicites ; cas 5/interdit refusés ; runtime texte réacquis |
| P2-J5 | Galerie, provenance et reprise | 20 publications/reprises sans doublon ; toutes les sorties ont les métadonnées requises |
| P2-J6 | Qualification du profil et distribution | Essai utilisateur reproduit et rapporté ; trois cycles de redémarrage/déplacement par profil revendiqué |

Les résultats mémoire, latence et qualité restent à enregistrer dans les [tests d’acceptation](tests_acceptation.md) ; un échec ne se transforme pas en certification partielle implicite.
Les composants non résolus, licences ou limites hors profil restent des décisions ouvertes dans [decisions.md](decisions.md).

## Références et suite

- [Index central](README.md), [phase 1](phase_1_socle_et_vision.md), [contrats transversaux](contrats_transversaux.md), [portabilité](portabilite.md).
- [Annexe 03 — FLUX.2 klein](annexes/03_flux_klein_generation.md), [annexe 02 — quantification](annexes/02_quantification_gguf.md), [annexe 06 — pratiques backend](annexes/06_bonnes_pratiques_python.md).
- [Pseudo-code images](pseudocode/backend_images.md), [stockage](pseudocode/backend_db.md), [socle](pseudocode/backend_socle.md), [interface](pseudocode/frontend.md) : descriptions non exécutables.
- Suite : [phase 3 — documents et voix](phase_3_documents_et_voix.md).
