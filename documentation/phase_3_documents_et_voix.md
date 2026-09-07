# Phase 3 — Documents, export local et voix

## Statut et résultat attendu

Cette phase spécifie la production de fichiers et l’audio utilisateur ; elle ne déclare pas ces fonctions déjà implémentées ou qualifiées.
L’utilisateur doit pouvoir obtenir un document ouvrable et traçable, l’exporter volontairement, dicter un brouillon modifiable et écouter un message choisi.
Les autorités sont les [contrats transversaux](contrats_transversaux.md), notamment C13 pour les documents, C14 pour la voix et C18 pour les limites de sécurité.

## Objectifs et hors périmètre

- Générer les quatre formats **TXT, Markdown, CSV et PDF** depuis des données structurées validées.
- Étendre la galerie aux documents sans dupliquer stockage, droits ou métadonnées des phases précédentes.
- Permettre téléchargement, copie/export vers un emplacement choisi et partage local explicitement déclenché.
- Ajouter une dictée appuyer/parler/arrêter, suivie d’une transcription finale insérée dans le brouillon avant envoi manuel.
- Ajouter la lecture vocale d’un message public sélectionné, avec arrêt et réglages proposés par le moteur retenu.

Hors périmètre : exécution de code rédigé par le LLM, formats Office natifs, macros, envoi automatique de fichiers, appels vocaux et agent vocal permanent.
La transcription continue est une amélioration ultérieure conditionnée aux mesures, non un prérequis de la dictée initiale ([C14](contrats_transversaux.md#c14--audio-utilisateur)).
Le partage local ne donne pas au modèle la permission d’envoyer un courriel, de publier sur Internet ou de sélectionner un destinataire.

## Prérequis et décisions d’entrée

1. Valider les services de phases 1–2 : contexte, outils, artefacts, galerie, droits, annulation et publication durable.
2. Définir les schémas de contenu, tailles maximales, encodages, styles PDF et contrôles de sortie.
3. Choisir les bibliothèques documentaires selon leurs capacités et licences, puis vérifier les dépendances natives et polices distribuées.
4. Identifier pour STT et TTS moteur, checkpoint, langues, voix, licence, checksum et backend du profil.
5. Fixer quotas CPU/RAM, taille et durée des enregistrements, politique des temporaires et stratégie d’arrêt.
6. Préparer un corpus de documents et d’audio versionné, incluant accents français, tableaux, bruit, silence et microphone indisponible.

Les bibliothèques et moteurs candidats sont référencés dans l’[annexe 05](annexes/05_generation_documents_python.md) et l’[annexe 04](annexes/04_audio_embarque.md), sans garantie de compatibilité de toutes leurs versions.
Tout choix de checkpoint, voix, ressource mémoire ou dépendance non validé reste ouvert dans [decisions.md](decisions.md) et doit être requalifié pour la [portabilité](portabilite.md).

## Responsabilités, entrées et sorties

| Responsable | Entrées | Sorties et frontière |
| :--- | :--- | :--- |
| Orchestrateur | Demande et données/sources accessibles | Appel `generate_file` typé, pas programme |
| Validateur documentaire | Union `txt`, `markdown`, `csv`, `pdf` | Objet borné ou erreur de schéma, de droits ou de contenu |
| Moteur déterministe | Données validées et styles prédéfinis | Fichier en préparation et diagnostics techniques |
| Service d’artefacts | Fichier vérifié et provenance | Publication, galerie et références de sortie après commit |
| Service d’export | ID autorisé et action utilisateur | Copie locale contrôlée ou annulation, sans envoi distant |
| Capture audio | Permission et commande utilisateur | Fichier temporaire contrôlé avec `recording_id` |
| Service STT | Enregistrement et paramètres du profil | Transcription finale, statut et éventuelles limites |
| Gestionnaire de brouillon | Texte transcrit, révision et cible | Insertion sûre ou proposition de fusion, jamais envoi automatique |
| Service TTS | Message public choisi, langue/voix disponibles | Audio lisible et arrêtable, sans traces internes |

La génération est un outil LLM ; la dictée et la lecture sont des actions utilisateur indépendantes de la boucle agentique ([C13–C14](contrats_transversaux.md#c13--outils-de-production-de-contenu)).
Ni `read_text` ni l’extraction PDF ne sont de la synthèse vocale ; les commandes et états doivent rester distincts.

## Contrat des documents et contrôles de contenu

| Format | Schéma de données attendu | Validation avant publication |
| :--- | :--- | :--- |
| TXT | Texte Unicode et nom proposé | Taille, encodage, caractères de contrôle et nom sûr |
| Markdown | Texte et références autorisées | Liens contrôlés, rendu sans HTML actif ni exécution |
| CSV | Colonnes, lignes, types et dialecte autorisé | Largeur des lignes, taille, encodage, valeurs et sécurité des formules |
| PDF | Titre, sections, paragraphes, tableaux et styles prédéfinis | Pagination, polices, marges, références et ouverture par un lecteur indépendant |

`generate_file` reçoit une union de schémas de **données** ; ne pas accepter Python, SQL, shell, HTML actif ou instructions exécutables du modèle.
Une demande de script comme contenu peut produire un TXT, mais ce contenu reste du texte inerte et n’est jamais exécuté par le moteur documentaire ([C13](contrats_transversaux.md#c13--outils-de-production-de-contenu)).
Les bibliothèques matérialisent le fichier ; elles ne garantissent ni justesse des chiffres, ni fidélité aux sources, ni absence de défaut de mise en page.
Vérifier séparément validité technique, conformité aux données d’entrée et exactitude factuelle appuyée sur des preuves ([C13](contrats_transversaux.md#c13--outils-de-production-de-contenu)).

Pour CSV, traiter les cellules textuelles commençant par `=`, `+`, `-` ou `@`, y compris après caractères de contrôle/espaces, selon une politique d’export explicitée.
Le simple échappement des guillemets CSV ne neutralise pas une formule de tableur ; prévoir neutralisation textuelle ou refus, et tester dans les lecteurs ciblés.
Distinguer un nombre négatif typé d’un texte potentiellement actif ; conserver les données d’origine et signaler toute transformation de sécurité.
Les documents joints et textes récupérés restent des données non fiables : une instruction qu’ils contiennent ne peut modifier le schéma, les droits ou le répertoire de sortie ([C18](contrats_transversaux.md#c18--sécurité-confidentialité-et-accessibilité)).

## Parcours concrets : succès et échec

### Produire un tableau et un rapport puis les exporter

1. L’utilisateur demande un CSV et un PDF à partir d’un tableau joint ; l’orchestrateur lit les seules données autorisées.
2. Deux sorties logiques sont préparées avec schémas validés, références et clés idempotentes distinctes.
3. Le backend contrôle les lignes CSV, les valeurs, les formules potentielles et le rendu du PDF.
4. Après publication, la réponse présente les IDs d’artefacts ; la galerie permet aperçu et ouverture.
5. L’utilisateur choisit « Exporter » et le répertoire ; confirmer un écrasement éventuel sans modifier les sources d’origine ([C09–C13](contrats_transversaux.md#c09--annulation-reprise-et-idempotence)).

Si le PDF échoue mais que le CSV est publié, annoncer un résultat partiel et conserver le CSV ; ne pas présenter les deux comme terminés.
Si un chemin est interdit, une police manque ou le disque est plein, retourner une erreur précise sans artefact fictif.
L’export ou la remise à une application locale suit une action utilisateur distincte ; aucune transmission distante automatique n’en découle.

### Dicter puis corriger avant envoi

1. L’utilisateur active le microphone, voit l’indicateur d’enregistrement, parle puis arrête.
2. Le backend reçoit une référence temporaire contrôlée, pas un gros flux audio encodé dans le JSON de contrôle.
3. Le STT produit le texte final ; l’interface rapproche `recording_id`, conversation cible et révision du brouillon.
4. Si le brouillon n’a pas changé, insérer la transcription à l’emplacement prévu ; sinon proposer une fusion sans écrasement.
5. L’utilisateur corrige les noms ou chiffres et choisit lui-même d’envoyer le message ([C08](contrats_transversaux.md#c08--ipc-et-état-de-linterface), [C14](contrats_transversaux.md#c14--audio-utilisateur)).

Permission refusée, périphérique absent, silence ou échec de modèle laissent la saisie texte disponible et le brouillon intact.
Une transcription tardive après changement de conversation ne doit ni apparaître dans l’autre brouillon ni déclencher un tour.

### Écouter une réponse et interrompre

1. L’utilisateur choisit un message public puis une voix/langue disponible ; le moteur reçoit une version nettoyée du balisage.
2. La lecture peut être arrêtée sans annuler le chat ; elle ne lit ni journal technique ni raisonnement interne.
3. Suspendre le microphone pendant la lecture dans le profil initial afin d’éviter le réenregistrement de la synthèse.
4. En cas de voix absente ou langue non prise en charge, expliquer le refus et garder le texte accessible ([C14](contrats_transversaux.md#c14--audio-utilisateur)).

## Audio : profil configurable, pas promesse universelle

La cible initiale est une exécution STT/TTS sur **CPU**, sans préempter la location GPU du texte ou de l’image.
Ce choix doit être un profil configurable et mesuré : moteur, threads, priorité, durée maximale et quotas CPU/RAM sont explicites.
La concurrence audio ne doit pas affamer l’interface ni un LLM lui-même exécuté sur CPU ; limiter ou sérialiser les tâches selon le profil ([C07](contrats_transversaux.md#c07--ressources-runtimes-et-concurrence)).
Ne pas garantir une empreinte RAM, un délai de transcription, une qualité de voix ou une couverture du français à partir du seul nom du moteur.
Langues, voix et précision dépendent du checkpoint et de sa version ; vérifier chaque combinaison revendiquée dans l’[annexe 04](annexes/04_audio_embarque.md) et le rapport de qualification.
Mesurer mémoire au pic, temps de chargement, temps par seconde d’audio et impact sur les autres tâches, à froid puis à chaud.
Toute évolution vers un autre backend audio exige une décision explicite et une nouvelle qualification ; elle ne constitue pas un repli silencieux.

## Données, confidentialité et reprise

Les documents réutilisent `Artifact`, `Blob`, `ArtifactLink`, le run et les références de données ; la galerie ajoute des filtres TXT/Markdown/CSV/PDF.
Conserver format, version de schéma, moteur documentaire, réglages et provenance ; caches et extractions restent exclus de la galerie par défaut ([C10](contrats_transversaux.md#c10--stockage-identifiants-et-droits)).
L’audio temporaire possède propriétaire, `recording_id`, date et échéance de suppression ; rendre la politique visible et conserver l’enregistrement seulement sur demande.
Ne pas faire entrer le son brut ou sa transcription intermédiaire dans l’historique du chat avant l’envoi utilisateur ([C14](contrats_transversaux.md#c14--audio-utilisateur)).
Après crash, traiter les fichiers préparatoires et enregistrements expirés sans supprimer une sortie publiée ou un document utilisateur.
Une relance ne duplique pas document ou export ; la clé d’effet et l’état durable permettent une réconciliation avant réexécution ([C09–C11](contrats_transversaux.md#c09--annulation-reprise-et-idempotence)).
Aucun moteur distant ne reçoit l’audio par défaut ; la voix reste facultative et ne remplace jamais l’accès clavier aux commandes ([C18](contrats_transversaux.md#c18--sécurité-confidentialité-et-accessibilité)).

## Exigences → conséquences → tests

Les seuils de performance du profil seront fixés avant campagne ; les critères fonctionnels ci-dessous ne sont pas encore mesurés.

| Exigence | Conséquence technique ou produit | Test d’acceptation proposé |
| :--- | :--- | :--- |
| Quatre formats — C13 | Schémas distincts, moteur déterministe | 5 cas de test par format ouvrables ; données attendues conservées et références présentes |
| Pas de code LLM exécuté — C13/C18 | Liste fermée de champs et styles | Toutes les entrées Python/shell/SQL/HTML actif sont refusées ou traitées comme texte inerte |
| CSV sûr — C18 | Politique de cellules textuelles dangereuses | Corpus `=`, `+`, `-`, `@`, espaces et tabulations : zéro formule exécutée dans les lecteurs ciblés |
| Export volontaire — C11 | Commande utilisateur et destination validée | Annuler ou refuser 10 exports : zéro écriture hors destination et zéro envoi réseau |
| Brouillon protégé — C14 | Corrélation enregistrement/révision | 20 retours tardifs ou concurrents : zéro écrasement, zéro envoi automatique |
| Audio CPU maîtrisé — C07/C19 | Quotas et profil par checkpoint | Mesurer pic RAM et latence sur 20 extraits ; aucune préemption GPU ni dépassement des quotas retenus |
| Lecture explicite — C14 | Message choisi, nettoyage et arrêt | 10 messages, dont listes et citations : aucune trace interne lue et arrêt dans le délai configuré |
| Reprise durable — C09/C11 | Temporaires séparés, effets idempotents | Crash à chaque étape de publication : zéro doublon et sorties partielles correctement annoncées |

## Jalons ordonnés et critères de sortie

| Ordre | Livraison attendue | Critère de passage à mesurer |
| :--- | :--- | :--- |
| P3-J1 | Schémas et politique de sécurité | Les 20 cas de test documentaires et toutes les entrées exécutables/dangereuses ont un résultat attendu |
| P3-J2 | Génération et galerie générale | TXT, Markdown, CSV, PDF publiés et ouverts ; aucun événement d’artefact avant commit |
| P3-J3 | Export et partage local contrôlé | Destination, refus, annulation et collision de nom testés ; aucune transmission automatique |
| P3-J4 | Capture et STT vers brouillon | Parcours appuyer/parler/arrêter/envoi manuel validé ; tous les conflits de révision préservent le texte |
| P3-J5 | TTS et coordination audio | Lecture/arrêt au clavier ; pas de boucle micro ; refus explicites des voix/langues indisponibles |
| P3-J6 | Qualification et distribution | Rapport CPU/RAM/latence/langue par checkpoint ; trois reprises après crash et déplacement réussies |

Les performances, langues effectivement validées et limites connues sont à publier dans les [tests d’acceptation](tests_acceptation.md), sans généralisation aux checkpoints non testés.

## Références et suite

- [Index central](README.md), [phase 2](phase_2_generation_image.md), [contrats C01–C19](contrats_transversaux.md), [portabilité](portabilite.md), [décisions](decisions.md).
- [Annexe 04 — audio embarqué](annexes/04_audio_embarque.md), [annexe 05 — documents](annexes/05_generation_documents_python.md), [annexe 06 — pratiques backend](annexes/06_bonnes_pratiques_python.md).
- [Pseudo-code audio et documents](pseudocode/backend_audio_documents.md), [stockage](pseudocode/backend_db.md), [interface](pseudocode/frontend.md) : renvois de conception, sans code LLM à exécuter.
- Suite : [phase 4 — moteur de recherche](phase_4_moteur_recherche.md).
