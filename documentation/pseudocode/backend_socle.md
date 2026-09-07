# Pseudo-code — Socle, transport et ressources

Cible conceptuelle non exécutée et non testée. Les [contrats C02, C07–C10, C18 et C19](../contrats_transversaux.md) gouvernent ce fichier ; le [contrat de portabilité](../portabilite.md) doit être lu avant de figer les chemins ou le packaging.

## Responsabilités et interfaces

- **QUI APPELLE** : le gestionnaire Rust démarre le sidecar ; le frontend lui adresse des commandes ; la boucle et les services demandent des ressources à l’ordonnanceur.
- **POURQUOI** : garder le contrôle disponible pendant un calcul natif et empêcher deux familles de pipelines de posséder simultanément le GPU du profil 8 Go.
- **NE PAS CONFONDRE** : coordinateur, worker isolé, location de ressource et handle natif ; quitter une attente ne prouve pas l’arrêt du calcul.
- **Entrées** : version de protocole, commande corrélée, racine de données autorisée, profil matériel/modèles, budget, demande d’annulation.
- **Sorties** : acceptation ou erreur de commande, événements ordonnés de run, état du backend et diagnostic borné sans secrets.
- **Phases** : [Phase 1] socle complet, droits et portabilité ; [Phase 2] famille image ; [Phase 3] quotas audio CPU ; [Phase 4] budgets réseau ; [Phase 5] périmètres d’espace, sans nouveau transport.

## Démarrage et récupération

```text
[Phase 1]
CONTRAT C02, C07, C08, C09, C10, C18, C19
QUI APPELLE : gestionnaire de cycle de vie Rust, décrit dans frontend.md.
POURQUOI : annoncer un backend utilisable, et non seulement un processus vivant.

AU DÉMARRAGE DU SIDECAR
    En portable_strict, résoudre la racine depuis l’emplacement du lanceur,
    jamais depuis le répertoire de travail ou le profil d’un ancien utilisateur.
    En mode installé explicite, résoudre la racine choisie pour ce mode distinct.
    Distinguer données durables, modèles, préparation temporaire et journaux.
    Vérifier accès, espace disque, liens symboliques et jonctions.
    SI racine invalide ou non inscriptible
        Retourner une erreur explicite ; ne pas choisir un dossier caché de repli.
        ARRÊTER avant toute mutation de la base.
    Acquérir le verrou applicatif de racine : une seule instance écrivante.
    SI verrou détenu, refuser ou proposer de rejoindre l’instance existante.
    Ne pas supprimer un verrou seulement d’après un ancien PID.
    Ouvrir le stockage et appliquer seulement les migrations de la phase active.
    Récupérer les runs non terminaux comme interrupted.
    À la resynchronisation, les signaler par run.failed, code RUN_INTERRUPTED,
    sans prétendre qu’un terminal avait été livré avant le crash.
    Réconcilier les effets enregistrés ; ne pas rejouer leurs écritures.
    Initialiser les files bornées de commandes, travaux et sorties.
    Démarrer un lecteur stdin indépendant des travaux d’inférence.
    Démarrer un sérialiseur stdout unique ; envoyer les logs sur stderr.
    Inventorier matériel, runtimes, modèles/checksums et capacités disponibles.
    SI un modèle manque
        Laisser l’historique consultable ; déclarer la capacité indisponible.
    Initialiser l’ordonnanceur sans charger toutes les familles de modèles.
    Annoncer backend.ready avec version et capacités réellement disponibles.
    Ne jamais déclarer un profil certifié sur la seule détection d’un pilote.
```

## Transport de contrôle non bloquant

Le vocabulaire exact de l’enveloppe et des événements vient de [C08](../contrats_transversaux.md). Les noms de commandes autres que `chat.send` décrits ci-dessous sont des opérations logiques à versionner, pas des API déjà présentes.

```text
[Phase 1]
CONTRAT C08, C09, C18
QUI APPELLE : lecteur de contrôle Rust vers stdin du coordinateur Python.
NE PAS CONFONDRE : réponse de commande et événement de progression d’un run.

PENDANT QUE LE SIDECAR EST OUVERT
    Lire des lignes UTF-8 sans attendre la fin du travail lourd.
    Assembler une ligne complète dans une limite de taille configurée.
    Valider un objet JSON, protocol_version, request_id, method et params.
    SI JSON invalide, version incompatible, méthode inconnue ou taille excessive
        Refuser sans exécuter ; garder le contrôle disponible si le flux est sain.
        Corréler l’erreur au request_id seulement s’il est exploitable.
    SINON
        Calculer l’empreinte canonique : protocol_version, méthode,
        acteur/périmètre et paramètres validés.
        Dédupliquer request_id avec cette empreinte complète.
        SI même identifiant avec empreinte différente
            Refuser avec REQUEST_CONFLICT.
        Résoudre ces clés dans le stockage durable, même après redémarrage.
        SI acceptation déjà enregistrée et empreinte identique
            Revalider les droits courants avant de restituer son résultat.
            Retourner l’acceptation existante sans réexécuter la commande.
        SI commande d’annulation, de statut ou de fermeture
            La traiter par le coordinateur, sans attendre la file d’inférence.
        SI commande chat.send
            Vérifier droits, pièces jointes et capacité de la file.
            Dans une même acceptation atomique, vérifier qu’aucun autre run chat
            non terminal n’existe pour cette conversation.
            SI la conversation est occupée, refuser sans insérer le nouveau message.
            SINON enregistrer en transaction courte le message utilisateur une fois,
            le Run de kind chat queued et l’acceptation idempotente.
            Une contrainte durable protège ce contrôle contre deux acceptations concurrentes.
            Renvoyer request_id, ok et result contenant run_id.
            Mettre le run en file ; la réponse finale viendra par événements.
        SI lecture d’historique ou de catalogue
            Passer au service de stockage ; ne pas lancer de modèle.
        SI file pleine
            Retourner une erreur récupérable ; ne pas accepter un travail perdu.

POUR CHAQUE SORTIE
    Les producteurs déposent une enveloppe dans la file de sortie.
    Regrouper les progrès coalescibles avant attribution de séquence.
    Le coordinateur attribue seq croissant par run et persiste son repère
    avant émission, avec l’état public correspondant à ce point de séquence.
    Au redémarrage, reprendre au-delà du repère durable ; ne pas repartir à zéro.
    Distinguer génération de connexion éphémère et continuité durable des seq.
    Le seul writer sérialise un objet par ligne UTF-8, puis effectue le flush.
    Une réponse porte request_id, ok, result OU error.
    Un événement de run porte protocol_version, event, run_id,
    conversation_id, seq et payload.
    conversation_id peut être nul pour un run de production du studio ;
    le champ reste présent et run_id reste la clé de corrélation.
    Une erreur porte code, message, retryable et details assainis.
    SI la sortie ralentit
        Appliquer la contre-pression aux producteurs et borner les tampons.
        Regrouper seulement des progrès non essentiels, jamais perdre un terminal.
        Réserver une capacité de contrôle pour annulation et fermeture.
    SI la sortie est coupée
        Cesser d’accepter de nouveaux travaux ; préserver l’état durable.
        Ne pas bloquer indéfiniment le nettoyage pour émettre un événement.

POUR UNE RESYNCHRONISATION
    Fournir une vue durable cohérente et son watermark par run.
    Inclure texte public confirmé, états et artefacts jusqu’à ce watermark.
    Sérialiser la prise de vue avec le repère de publication d’événements.
    Ne pas annoncer dans le watermark des deltas absents de la vue retournée.
    L’UI tamponne les événements pendant cette lecture, remplace sa projection,
    puis applique seulement ceux qui suivent le watermark dans l’ordre.
    Signaler interrupted récupéré même si l’ancien compteur UI était plus élevé ;
    la vue durable de reprise reste l’autorité.
```

## Ordonnanceur et location GPU exclusive

```text
[Phase 1] [Phase 2]
CONTRAT C04, C07
QUI APPELLE : chaque inférence texte, vision/OCR ou image.
POURQUOI : un runtime déchargé ne peut plus servir le tour suivant.
NE PAS CONFONDRE : famille de pipeline et nombre de composants internes.

POUR ADMETTRE UN RUN LOURD AUTORISÉ
    Réserver la place de run lourd unique du profil portable initial.
    Laisser les autres conversations consultables ; borner leurs demandes en file.
    Attribuer un jeton d’admission UNE FOIS au run, avant boucle et compaction.
    Ses outils et sous-recherches réutilisent cette admission jusqu’à la clôture.
    Ne pas réacquérir la place de run lourd depuis une sous-opération.
    Un travail autonome d’indexation demande sa propre admission hors d’un run actif.

POUR CHAQUE INFÉRENCE DU RUN ADMIS
    Vérifier le jeton d’admission parent.
    SI profil GPU, attendre sa location exclusive dans le budget d’attente.
    SINON réserver la capacité runtime CPU du profil qualifié ;
    ne pas attendre un GPU inexistant ni présumer les mêmes performances.
    Dans les deux cas, garder propriétaires, génération et nettoyage explicites.
    Interdire toute acquisition imbriquée, même par le propriétaire courant.
    Noter propriétaire, famille demandée et génération de handle.
    SI une autre famille est résidente
        Passer de ready à unloading ; invalider les anciens handles.
        Demander la fermeture native et vérifier le nettoyage dans un délai borné.
        SI fermeture non confirmée
            Terminer le worker isolé ; attendre sa sortie effective.
        SI la sortie ou la libération restent incertaines
            Passer à failed ; interdire tout nouveau chargement GPU.
            ARRÊTER le travail avec diagnostic et action de récupération.
        SINON passer à unloaded.
    SI aucune famille compatible n’est prête
        Passer à loading ; vérifier fichiers, architecture, projecteur requis,
        format, contexte et budget mesuré de tous les composants nécessaires.
        Charger dans un worker isolable, puis passer à ready.
    Attribuer une location et un handle de génération courante.
    Passer à running ; exécuter uniquement le travail de cette location.
    À LA FIN OU À L’ERREUR
        Confirmer la fin native avant de rendre la location.
        Invalider le jeton de location ; revenir à ready si runtime sain.
        Un pipeline peut rester résident sans conserver de location active.
        Sinon nettoyer jusqu’à unloaded ou bloquer en failed.

APRÈS UNE DÉCISION TEXTE
    Libérer la location texte AVANT d’exécuter un outil vision ou image.
    Ne conserver aucun handle texte dans la portée de l’outil.
    APRÈS L’OUTIL, réacquérir le texte avec une nouvelle génération vérifiée.
    Reconstruire son entrée depuis SharedContext et la projection du tour.
    L’admission du run reste détenue ; seule la location de runtime change.
```

## Annulation, fermeture et qualification

```text
[Phase 1]
CONTRAT C07, C08, C09, C19
QUI APPELLE : utilisateur, fermeture Rust, budget dépassé ou superviseur.

À LA DEMANDE D’ANNULATION
    Marquer l’intention et l’état cancelling ; retirer le travail s’il attend en file.
    Sinon signaler l’arrêt coopératif au worker.
    Attendre la confirmation native dans le délai configurable.
    SI aucun arrêt confirmé
        Escalader au processus worker isolé ; attendre sa sortie.
    Tant que le worker peut encore calculer, ne pas réattribuer sa ressource.
    Réconcilier les artefacts déjà publiés et les résultats d’outils.
    Persister l’état final avant le terminal unique du run.
    Ne pas démarrer automatiquement le générateur après une annulation.

À LA FERMETURE
    Refuser les nouveaux runs et informer les commandes en attente.
    Annuler les travaux, fermer les workers et confirmer leur sortie.
    Fermer stockage et writer après drainage borné ; signaler les échecs.
    Relâcher le verrou de racine après fermeture des écritures.
    SI le sidecar tombe brutalement, Rust gère la fin de son arbre de workers.

À LA COMMANDE « PRÉPARER LE DÉPLACEMENT »
    Arrêter les admissions, attendre ou annuler les travaux.
    Confirmer la fin des workers et la fermeture cohérente de la base et des journaux.
    Déclarer le dossier prêt seulement lorsqu’aucune écriture ne reste active.
    Ne jamais conseiller de copier seulement le fichier de base pendant un run.

[Phase 3]
    Exécuter l’audio dans des workers CPU sous quotas CPU/RAM.
    Ne pas remplacer l’isolation par un thread déclaré « annulé ».

[Phase 1] [Phase 2] [Phase 3] [Phase 4] [Phase 5]
    Qualifier chaque profil avec OS, architecture, instructions CPU, pilote,
    runtime, modèle/checksum, quantification, contexte et mesures.
    Séparer VRAM dédiée, mémoire unifiée, RAM, disque et fichiers mappés.
    Proposer explicitement un autre profil en cas de limite.
    Ne jamais retirer un projecteur ni changer d’architecture en silence.
```

## Erreurs et arrêts attendus

Les codes précis restent à stabiliser ; les catégories doivent distinguer indisponibilité de backend, modèle absent, format incompatible, hors mémoire, file pleine, protocole invalide, stockage indisponible et nettoyage non confirmé. Aucune de ces erreurs ne justifie une réussite fictive ou un lancement parallèle de secours. [C07–C09, C18–C19](../contrats_transversaux.md)

## Tests d’acceptation à rattacher

Scénarios associés aux tests **T-C02, T-C08, T-R01 à T-R03, T-R05, T-R07, T-R09, T-A05 et T-P01 à T-P12** de la [matrice d’acceptation](../tests_acceptation.md), tous à exécuter.

- [Phase 1] Pendant un appel natif lent, demander statut puis annulation : les commandes sont reçues ; aucune location suivante avant arrêt confirmé.
- [Phase 1] Fournir lignes partielles, JSON invalide, doublons et file pleine : refus explicites, writer unique et mémoire bornée.
- [Phase 1] Crash après commit avant accusé de réception : même requête, même acceptation, aucun second message.
- [Phase 1] Texte puis OCR puis texte : ancien handle rejeté, location non imbriquée et reprise sur nouvelle génération.
- [Phase 2] Échec de fermeture image : aucun chargement texte tant que le worker subsiste.
- [Phase 3] Audio et inférence CPU : quotas et contrôles restent disponibles ; aucun faux arrêt par timeout de thread.
- [Phase 1] Déplacer la racine portable puis relancer : références durables retrouvées sans chemin développeur.
- [Phase 1] **T-R10, T-R11, T-R12** : admission non imbriquée, second chat refusé avant insertion, watermark durable sans perte de terminal récupéré.

## Navigation

[Sommaire](README.md) · [Boucle](backend_agent_loop.md) · [Stockage](backend_db.md) · [Frontend et Rust](frontend.md). Appuis techniques, non garanties produit : [annexe GGUF](../annexes/02_quantification_gguf.md) et [annexe Python](../annexes/06_bonnes_pratiques_python.md).
