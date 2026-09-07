# Pseudo-code — Documents, dictée et lecture vocale

Cible conceptuelle non exécutée et non testée. Les [contrats C02, C07–C11, C13, C14, C18 et C19](../contrats_transversaux.md) séparent la génération de fichiers par outil et la voix pilotée par l’utilisateur.

## Responsabilités et interfaces

- **QUI APPELLE** : l’exécuteur pour `generate_file` ; les commandes utilisateur via Rust pour la dictée et la lecture vocale.
- **POURQUOI** : matérialiser du contenu structuré sans exécuter un programme LLM et préserver le brouillon pendant une transcription.
- **NE PAS CONFONDRE** : générer un document, extraire son texte, transcrire une voix et lire un message à voix haute ; seul le premier est ici un outil d’orchestration.
- **Entrées** : format autorisé et contenu structuré ; ou `recording_id`, révision de brouillon et référence audio contrôlée ; ou `message_id`, voix/langue et paramètres utilisateur.
- **Sorties** : artefacts documentaires enregistrés ; transcription finale modifiable ; références temporaires audio autorisées pour lecture ; erreurs corrélées et bornées.
- **Phases** : [Phase 3] TXT, Markdown, CSV, PDF, export, galerie générale et voix ; réutilisation des droits et de l’ordonnanceur de [Phase 1].

## Schéma de génération documentaire

```text
[Phase 3]
CONTRAT C10, C13, C18
QUI APPELLE : outil generate_file validé par l’exécuteur.
POURQUOI : le modèle décrit le contenu ; le backend choisit un writer déterministe.

VALIDER L’UNION DE SCHÉMAS
    txt : texte UTF-8 borné.
    markdown : texte borné avec balisage admis et références autorisées.
    csv : colonnes et lignes structurées, types et dimensions vérifiés.
    pdf : titre, sections, paragraphes, tableaux et styles prédéfinis.
    Refuser type absent, variante inconnue et champs supplémentaires.
    Limiter longueur, pages, cellules, images, styles et références.
    Refuser code Python, SQL, shell, HTML actif ou expressions à exécuter.
    Un extrait de code demandé comme texte reste une donnée inerte.
    Revalider chaque artifact_id et evidence_id dans la portée du run.
    Vérifier noms sûrs ; le modèle ne choisit pas un chemin de sortie.
    Ne pas autoriser le writer à aller chercher une ressource distante implicite.
```

## Flux principal de production et d’export

```text
[Phase 3]
CONTRAT C09, C11, C13
QUI APPELLE : exécuteur, puis service de publication du stockage.

POUR UNE DEMANDE generate_file ACCEPTÉE
    Consulter la clé idempotente et les sorties déjà publiées.
    SI résultat existant, revalider les droits puis le retourner sans second fichier.
    SINON
        Réserver les quotas CPU/RAM/disque nécessaires.
        Écrire dans une zone de préparation gérée.
        SI txt, écrire du texte avec encodage déclaré.
        SI markdown, produire un fichier inerte ; neutraliser le HTML actif au rendu.
        SI csv, sérialiser colonnes et cellules avec échappement cohérent.
            Appliquer une politique explicite aux cellules interprétables
            comme formules par un tableur, sans altération silencieuse.
        SI pdf, utiliser les blocs et styles prédéfinis du writer.
            Borner la pagination, vérifier polices et rendu des caractères.
        Fermer le writer puis rouvrir le fichier pour vérifier le format.
        Contrôler nombre de lignes/pages, références, montants et totaux
        lorsque des données structurées permettent une vérification déterministe.
        Séparer validité technique, fidélité aux entrées et exactitude factuelle.
        Signaler ce qui n’a pas été vérifié ; ne pas promettre « toujours exact ».
        Publier via Blob, Artifact et ArtifactLink en transaction courte.
        Après commit, émettre artifact.created.
    Retourner au tour une référence d’artefact, un aperçu et les limites.
    Laisser la boucle enregistrer le résultat unique de l’outil.

POUR EXPORTER
    Attendre une commande utilisateur et une destination choisie via Rust.
    Revalider les droits, les dépendances et le nom du fichier.
    Copier l’artefact publié vers cette destination après confirmation requise.
    Ne pas modifier l’artefact d’origine ni remplacer un document utilisateur
    sur la seule proposition du modèle.
```

## Dictée finale vers un brouillon révisable

```text
[Phase 3]
CONTRAT C07, C08, C09, C14, C18
QUI APPELLE : utilisateur, par appuyer / parler / arrêter.
NE PAS CONFONDRE : entrée audio et message de chat automatiquement envoyé.

À LA DEMANDE DE CAPTURE
    Vérifier permission micro et périphérique, puis créer recording_id.
    Noter conversation active, identité du brouillon et révision de départ.
    Annoncer l’état d’enregistrement sans dépendre uniquement de la couleur.
    Capturer dans un fichier temporaire géré avec limites de durée et de taille.
    Ne pas transporter des octets audio ou du base64 dans JSON Lines.
    Prévoir arrêt et abandon disponibles au clavier.

QUAND L’UTILISATEUR ARRÊTE
    Fermer le fichier et transmettre une référence temporaire contrôlée,
    recording_id, format déclaré et révision du brouillon via Rust.
    Vérifier côté backend propriété, durée, taille, format réel et chemin.
    Mettre le travail STT dans une file CPU bornée.
    Lancer un worker CPU isolable sous quotas, sans location GPU.
    Le lecteur stdin et le writer stdout restent disponibles.
    Produire une transcription finale, pas un flux continu promis d’emblée.
    Renvoyer le texte et sa qualité avec recording_id et révision de départ
    dans le résultat corrélé de la commande.

À RÉCEPTION DANS L’INTERFACE
    SI recording_id n’est plus actif, ou brouillon/conversation ont changé
        Ne pas insérer automatiquement ; proposer le texte comme résultat séparé.
    SINON SI la révision du brouillon est inchangée
        Insérer la transcription dans le brouillon éditable.
    SINON
        Préserver le texte modifié ; proposer insérer, remplacer ou ignorer.
    Ne jamais appeler chat.send automatiquement.
    Attendre validation et envoi manuel de l’utilisateur.
```

Les commandes audio restent logiquement distinctes des événements de run et des outils LLM. Leur nomenclature et leur éventuelle progression dédiée sont à versionner ; une transcription finale peut être retournée comme réponse asynchrone corrélée sans bloquer le coordinateur. [C08 et C14](../contrats_transversaux.md)

## Lecture vocale d’un message choisi

```text
[Phase 3]
CONTRAT C07, C08, C14, C18
QUI APPELLE : bouton de lecture d’un message public sélectionné.
POURQUOI : ne pas lire les traces techniques ni réenregistrer sa propre voix.

À LA DEMANDE DE LECTURE
    Résoudre message_id autorisé et sa révision de contenu.
    Lire uniquement son texte public, jamais un journal ni un canal privé.
    Nettoyer le balisage de façon prévisible sans modifier faits et nombres.
    Vérifier langue, voix, vitesse et volume selon capacités du moteur.
    SI micro actif, le suspendre ou demander l’arrêt avant la lecture.
    Segmenter un message long en portions bornées et identifiables.
    Produire chaque portion dans un worker TTS CPU isolable, sous quotas.
    Écrire l’audio dans un fichier temporaire géré puis le valider.
    Renvoyer une référence contrôlée, type audio, durée et index de portion.
    Le bus JSON ne contient jamais les octets audio.
    L’UI ouvre la référence via le pont de médias autorisé et lit dans l’ordre.
    Garder arrêt, volume, vitesse et retour au texte disponibles.
    Ne pas réactiver automatiquement le micro si cela risque une boucle.

SI l’utilisateur change de message ou arrête
    Arrêter la lecture locale immédiatement ; demander annulation des travaux.
    Ignorer les références tardives d’une lecture antérieure.
    Attendre l’arrêt natif avant de réutiliser le worker ou sa ressource.
```

## Erreurs, annulation et conservation

```text
[Phase 3]
CONTRAT C07, C09, C11, C13, C14

SI permission refusée ou périphérique absent
    Conserver le brouillon ; proposer saisie clavier et réglage de permission.
SI silence ou bruit inexploitable
    Annoncer absence de transcription fiable, pas un texte inventé.
SI modèle audio absent, langue non disponible ou quota dépassé
    Retourner une erreur distincte ; ne pas basculer vers un service distant.
SI annulation d’un calcul natif documentaire ou audio
    Signaler coopérativement, puis terminer le worker isolé après délai.
    Ne pas considérer un thread ou une coroutine abandonnée comme arrêt réel.
SI génération documentaire échouée
    Ne pas publier un fichier invalide ; garder une erreur corrélée à l’outil.
SI échec après commit
    Réutiliser l’artefact enregistré lors de la réconciliation.

À LA FIN D’UN ENREGISTREMENT OU D’UNE LECTURE
    Appliquer la politique visible de rétention des fichiers temporaires.
    Supprimer seulement lorsque aucun travail/lecteur ne les utilise encore.
    Conserver durablement l’audio uniquement sur demande explicite.
    Si conservation demandée, publier comme artefact avec droits et liens.
    Exclure ces temporaires des identifiants durables et sauvegardes ordinaires.
```

## Tests d’acceptation à rattacher

Scénarios associés à **T-D01 à T-D05, T-A01 à T-A06, T-R02 et T-R07** dans la [matrice de tests](../tests_acceptation.md), à exécuter.

- [Phase 3] TXT, Markdown, CSV et PDF valides : fichiers ouvrables, données fidèles, références résolues et artefacts visibles après commit.
- [Phase 3] Schéma contenant un programme, une destination libre ou HTML actif : rejet ou traitement en texte inerte, aucune exécution.
- [Phase 3] PDF techniquement valide mais total incohérent : contrôle distinct, pas de certification factuelle automatique.
- [Phase 3] Répétition de génération ou export interrompu : aucune duplication ni écrasement non confirmé.
- [Phase 3] Transcription en retard après édition du brouillon ou changement de conversation : aucun écrasement et aucun envoi automatique.
- [Phase 3] Silence, bruit, permission refusée et périphérique absent : erreurs actionnables, saisie clavier préservée.
- [Phase 3] Inspecter le contrôle IPC : seulement références audio, pas d’octets ni de base64.
- [Phase 3] Micro actif pendant TTS puis annulation native : pas de boucle de réenregistrement ; quotas et arrêt effectif vérifiés.
- [Phase 3] Purge des temporaires et conservation volontaire : politique respectée sans référence durable vers un fichier supprimé.

## Navigation

[Sommaire](README.md) · [Socle](backend_socle.md) · [Stockage](backend_db.md) · [Frontend](frontend.md) · [Phase 3](../phase_3_documents_et_voix.md). Appuis techniques : [annexe audio](../annexes/04_audio_embarque.md) et [annexe génération de documents](../annexes/05_generation_documents_python.md).
