# Pseudo-code — Interface, pont Rust et cycle de vie

Cible conceptuelle non exécutée et non testée. Les [contrats C02, C05, C08–C19](../contrats_transversaux.md) définissent l’autorité backend, les états publics et les parcours accessibles ; le [contrat de portabilité](../portabilite.md) régit lancement, fichiers et fermeture.

## Responsabilités et interfaces

- **QUI APPELLE** : utilisateur vers React, React vers commandes Tauri/Rust, Rust vers sidecar Python ; les événements reviennent par le même pont.
- **POURQUOI** : laisser la navigation et l’annulation disponibles sans confondre commande acceptée, réponse en cours et résultat durable.
- **NE PAS CONFONDRE** : état d’interface et état du run, statut de message et terminal de run, historique et galerie, transport Rust et second backend de stockage.
- **Entrées** : intentions utilisateur, références contrôlées d’import, acceptations de commandes, événements versionnés et vues durables obtenues via le backend.
- **Sorties** : commandes corrélées, pages et composants accessibles, progrès bornés, brouillons révisables et erreurs actionnables.
- **Phases** : [Phase 1] chat/historique/imports/diagnostic ; [Phase 2] studio/galerie ; [Phase 3] documents/export/voix ; [Phase 4] recherche/sources/cache ; [Phase 5] espaces/mémoire.

## Démarrage et cycle de vie du sidecar Rust

```text
[Phase 1]
CONTRAT C07, C08, C09, C18, C19
QUI APPELLE : ouverture de la fenêtre et gestionnaire Rust unique de l’application.
POURQUOI : démarrer un vrai service surveillé, pas retourner une réussite si absent.

À L’OUVERTURE
    Afficher un diagnostic de démarrage accessible sans terminal.
    Résoudre lanceur, binaire sidecar et racine selon le mode portable déclaré.
    Vérifier version du paquet, chemins autorisés et présence du sidecar.
    Démarrer une seule instance supervisée avec arguments contrôlés.
    Ne pas invoquer du Python système ou un script SQL comme solution de secours.
    Attacher les lecteurs stdout et stderr et le suivi de sortie du processus.
    Attendre backend.ready dans un délai configuré.
    Vérifier version négociée et capacités annoncées.
    SI modèle absent mais stockage ouvert
        Autoriser consultation de l’historique et expliquer la fonction indisponible.
    SI sidecar absent, démarrage échoué ou protocole incompatible
        Afficher indisponibilité, diagnostic borné et action réessayer.
        Ne pas valider une commande comme réussie.

POUR LE CONTRÔLE RUST
    Garder les canaux de lecture et d’écriture disponibles pendant l’inférence.
    Assembler stdout par lignes complètes, même si un paquet coupe une ligne UTF-8.
    Borner la longueur, valider l’enveloppe et relayer uniquement le contrôle.
    Traiter stderr comme diagnostic borné sans secrets, jamais comme événements.
    Sérialiser les commandes vers stdin pour éviter l’entrelacement.
    Maintenir une table de requêtes en attente et la génération de connexion.
    SI le processus sort
        Échouer les commandes non résolues avec état de livraison incertain.
        Conserver les run_id connus ; ne pas les marquer completed arbitrairement.
        Afficher déconnexion et préparer une resynchronisation.
```

## Commandes, historique et imports

```text
[Phase 1]
CONTRAT C08, C09, C10, C11, C18
QUI APPELLE : navigation, composition et actions explicites de l’utilisateur.
NE PAS CONFONDRE : import confirmé par le backend et simple aperçu de brouillon.

POUR UNE COMMANDE
    Former protocol_version, request_id stable, method et params validés.
    Garder version, méthode, acteur/périmètre et paramètres identiques
    lors d’un réessai transport ; l’empreinte backend inclut ces champs.
    Envoyer via Rust ; recevoir request_id, ok, result OU error.
    SI REQUEST_CONFLICT, afficher le conflit ; ne pas changer les paramètres
    sous le même identifiant pour forcer une acceptation.
    Si l’achèvement est incertain, consulter l’état backend avant toute relance.

POUR CONSULTER HISTORIQUE, MESSAGES, GALERIE OU SOURCES
    Demander une page au backend via Rust.
    Trier selon l’ordre stable renvoyé et conserver le curseur de pagination.
    Distinguer résultat vide, chargement et erreur.
    Ne pas ouvrir SQLite depuis l’UI.
    Rust ne contourne pas le sidecar par ses propres requêtes SQL ou scripts.

POUR IMPORTER IMAGE, PDF OU TEXTE
    Accepter sélection fichier, bouton accessible ou dépôt.
    Effectuer une première validation UI de type et taille avec erreur par fichier.
    Obtenir via Rust une référence temporaire contrôlée, pas un chemin LLM.
    Faire valider et copier le fichier dans le stockage géré par le backend.
    Attendre artifact_id et rattachement autorisé avant de le considérer importé.
    Garder ordre de sélection et nom accessibles pour lever les ambiguïtés.
    Ne pas intégrer d’image en base64 ni de document entier dans le contrôle JSON.

POUR ENVOYER UN CHAT
    Conserver le brouillon jusqu’à acceptation.
    Transmettre conversation_id si existante, texte et artifact_ids autorisés.
    Le backend crée si nécessaire une Conversation et persiste le message une fois.
    Il refuse atomiquement un second chat non terminal de la même conversation,
    sans insérer le message refusé ; conserver ce brouillon pour un envoi ultérieur.
    chat.send retourne une acceptation et run_id, pas la réponse finale.
    Associer l’interface à ce run de kind chat et à sa conversation.
    Un double clic ou réessai réseau ne crée pas un second message.
    Autoriser consultation d’autres conversations pendant l’attente du run lourd.
```

## États, tri et déduplication des événements

```text
[Phase 1] [Phase 2]
CONTRAT C05, C08, C09
QUI APPELLE : répartiteur central d’événements, pas chaque composant isolément.
POURQUOI : une réponse tardive ne doit jamais apparaître dans la conversation ouverte.

CONSERVER UN ÉTAT PAR run_id
    kind, conversation_id éventuellement nul pour production, état durable,
    dernier seq appliqué, étapes, outils par tool_call_id, artefacts par artifact_id.
    États de run : queued, running, cancelling, completed, cancelled, failed,
    interrupted après récupération.
    Statuts de message : complete, partial, cancelled ou failed.
    Ne pas traduire le statut partial d’un message en « génération en cours ».

POUR CHAQUE ÉVÉNEMENT
    Vérifier protocol_version, event, run_id, conversation_id, seq et payload.
    Vérifier la génération de connexion et le run connu.
    SI événement précède l’acceptation
        Le retenir dans un tampon borné ou demander une resynchronisation.
        Ne pas l’attribuer à la conversation active par défaut.
    SI seq déjà appliqué ou événement d’une connexion périmée
        Ignorer le doublon ou l’événement obsolète.
    SI seq laisse un trou
        Réordonner dans un tampon borné ; si le trou persiste, resynchroniser.
        Ne pas concaténer les deltas dans leur seul ordre d’arrivée.
    SI conversation_id ne correspond pas à l’association du run
        Refuser l’événement et demander l’état autoritatif au backend.
    SI run de production avec conversation_id nul
        Mettre à jour le studio par run_id ; ne pas fabriquer une conversation.

    SI run.started, afficher le run running.
    SI run.stage, afficher l’action publique, jamais une trace de raisonnement.
    SI tool.started OU tool.completed, mettre à jour l’outil corrélé une fois.
    SI answer.delta, ajouter uniquement au message en cours du bon run.
    SI artifact.created, ajouter ou actualiser l’artefact après commit backend.
    SI run.completed, run.cancelled OU run.failed
        Appliquer le terminal logique une seule fois.
        Relire au besoin le message durable avec son vrai statut.
        Ne pas accepter un delta tardif qui réouvre un run terminé.
    Mettre à jour dernier seq appliqué seulement après application cohérente.

AU CHANGEMENT DE PAGE OU DE CONVERSATION
    Changer la projection affichée, pas l’identité des événements.
    Garder les travaux non visibles dans le magasin de runs.
    Libérer les abonnements locaux devenus inutiles.
    Maintenir un seul répartiteur global ; éviter les doubles abonnements.
```

Les noms historiques `answer`, `message_completed`, `tool_result` ou `backend_ready` nécessitent un adaptateur de version explicite et testé ; on ne leur invente pas `run_id`, ordre ou statut manquants. Les canaux sont séparés avant affichage, sans supprimer arbitrairement du français ou du texte entre crochets. [C05 et C08](../contrats_transversaux.md)

## Parcours image, documents et audio

```text
[Phase 2]
CONTRAT C02, C08, C10, C13
QUI APPELLE : navigation Studio et Galerie.

POUR LE STUDIO
    Afficher prompt, références ordonnées, réglages qualifiés et bouton d’arrêt.
    Valider les limites du profil, dont quatre références au maximum à certifier.
    Envoyer une commande directe au service image, sans orchestrateur.
    Suivre un run de production, même sans conversation_id.
    Afficher la bibliothèque personnelle par la même vue d’artefacts.
    Attacher une création à une conversation seulement après sélection explicite.
    La bibliothèque personnelle n’est pas consultable automatiquement par tous les chats.
    Afficher étapes mesurées ; utiliser une progression indéterminée sans total fiable.

POUR LA GALERIE
    Demander métadonnées et références de vignettes autorisées au backend.
    Dédupliquer artifact_id et paginer ; filtrer explicitement les imports.
    Ouvrir, sélectionner comme référence ou exporter par commandes contrôlées.
    Ne pas créer une seconde base ou un second pipeline image dans l’interface.

[Phase 3]
CONTRAT C11, C13, C14
POUR LES DOCUMENTS
    Étendre la même galerie à TXT, Markdown, CSV et PDF.
    Afficher aperçu inerte, provenance et limites de validation.
    Exporter via destination utilisateur, avec choix avant remplacement.
POUR LA DICTÉE
    Demander permission, enregistrer, arrêter et afficher transcription finale.
    Transmettre recording_id, révision du brouillon et référence audio contrôlée.
    Ne pas transmettre des octets audio arbitraires dans JSON.
    SI le brouillon a changé, demander quoi faire du texte reçu.
    Attendre un envoi manuel ; garder la saisie clavier disponible.
POUR LA LECTURE VOCALE
    Choisir un message public ; demander des références audio au service TTS.
    Lire les fichiers temporaires autorisés, avec arrêt, volume et vitesse.
    Suspendre le micro si nécessaire ; ignorer les portions d’une ancienne lecture.
```

## Parcours recherche et gestion des sources

```text
[Phase 4]
CONTRAT C12, C15, C16, C18
QUI APPELLE : utilisateur depuis le chat ou le panneau de recherche.
POURQUOI : rendre le périmètre et les limites compréhensibles avant le réseau.

AVANT LA RECHERCHE
    Afficher et transmettre le mode local_only, web_allowed ou deep_research.
    Montrer les sources sélectionnées et les budgets ; ne pas élargir en silence.
    Respecter « uniquement ce PDF », même si un bouton web était précédemment actif.
    Demander confirmation du transfert sensible avec contenu minimal et destination.
    Si une clé est nécessaire, proposer saisie par session ou stockage OS sécurisé ;
    expliquer qu’elle ne voyage pas par défaut avec le dossier portable.

PENDANT LA RECHERCHE
    Afficher étapes publiques : fichiers, cache, réseau, lecture de pages.
    Montrer couverture et limites bornées, sans chaîne de pensée.
    Garder annulation et navigation disponibles.
    Paginer les résultats pertinents plutôt que limiter la recherche aux plus populaires.

POUR PRÉSENTER LA RÉPONSE
    Afficher les citations validées et leurs localisations.
    Rendre les liens sans HTML actif et ouvrir les sources par accès autorisé.
    Distinguer résultat complet, partiel, ancien, contradictoire et inaccessible.
    Ne pas présenter une fiche descriptive comme une preuve déjà lue.

POUR « SOURCES ET CACHE »
    Montrer snapshots, URL demandée/finale, dates et état de fraîcheur.
    Autoriser inspection, revalidation autorisée et purge confirmée.
    Avertir des dépendances utilisées par des réponses antérieures.
    Ne pas mélanger ces snapshots aux créations dans la galerie par défaut.
```

## Parcours espaces et mémoire

```text
[Phase 5]
CONTRAT C10, C11, C17
QUI APPELLE : sélecteur d’espace et commandes utilisateur de gestion.

POUR NAVIGUER
    Garder visibles conversations hors espace et espaces autorisés.
    Présenter fichiers utilisateur et mémoire comme zones logiques distinctes.
    La zone utilisateur est modifiable par l’utilisateur, en lecture seule pour l’IA.
    Afficher liens et portée, pas des chemins absolus.
POUR PARTAGER OU DÉPLACER
    Demander la portée cible et expliquer les dépendances.
    Appeler le backend pour créer/retirer les liens et recalculer les droits.
    Ne pas transférer de mémoire privée à travers un simple changement de page.
    SI le backend ne démontre pas l’indépendance des anciens messages/snapshots
        Afficher le refus de déplacement et proposer une nouvelle conversation
        avec sélection explicite de contenus autorisés ; garder l’original intact.
POUR LA MÉMOIRE
    Montrer candidate, accepted, stale et rejected, versions et sources.
    Présenter les actions valider, refuser, corriger, invalider, fusionner et supprimer.
    Confirmer la promotion et tout partage de dépendances nécessaire.
    Classer par pertinence puis utilité secondaire ; proposer page suivante
    et recherche ciblée au-delà de cinq fiches.
    Ne pas afficher « fiable » parce qu’une fiche est souvent consultée.
```

## Annulation, reprise et fermeture

```text
[Phase 1] [Phase 2] [Phase 3] [Phase 4] [Phase 5]
CONTRAT C07, C08, C09, C19
QUI APPELLE : arrêt utilisateur, perte du sidecar ou fermeture de l’application.

À L’ANNULATION
    Envoyer la commande corrélée au run.
    Afficher cancelling tant que l’arrêt natif n’est pas confirmé.
    Garder visibles le texte partiel et les artefacts déjà publiés.
    Ne pas annoncer cancelled uniquement parce qu’une attente UI a expiré.

APRÈS DÉCONNEXION OU REDÉMARRAGE
    Négocier la nouvelle version et génération de connexion.
    Tamponner de façon bornée les événements arrivant pendant la resynchronisation.
    Demander au backend une vue durable cohérente : états, messages, artefacts
    et watermark par run, issu des repères seq persistés.
    Remplacer atomiquement les projections locales périmées par cette vue.
    Dédupliquer les ressources et abandonner les événements jusqu’au watermark inclus.
    Appliquer ensuite, dans l’ordre, seulement les événements au-delà du watermark.
    SI débordement du tampon ou nouveau trou, refaire une synchronisation bornée.
    Ne pas confondre génération de connexion locale et continuité durable des seq.
    Ne pas laisser un vieux compteur UI masquer le statut récupéré d’un run.
    Si interrupted, afficher l’interruption récupérée.
    Accepter run.failed avec code RUN_INTERRUPTED comme signal de reprise,
    pas comme preuve qu’un événement avait été livré avant le crash.
    Proposer une relance explicite sans dupliquer le message utilisateur.
    Ne pas rejouer une production ou une recherche dont l’effet est incertain.

À LA FERMETURE OU « PRÉPARER LE DÉPLACEMENT »
    Demander attendre ou annuler les travaux en cours.
    Rust commande l’arrêt, continue de lire le protocole et attend la fermeture.
    Après délai, faire escalader l’arrêt des workers par le superviseur.
    Si sidecar défaillant, terminer son arbre de processus et attendre sa sortie.
    Ne pas annoncer le dossier déplaçable avant fin de calcul et écritures confirmées.
    Libérer les abonnements, références médias et verrous après fermeture réelle.
```

## Accessibilité et erreurs actionnables

```text
[Phase 1] [Phase 2] [Phase 3] [Phase 4] [Phase 5]
CONTRAT C18
QUI APPELLE : tous les composants et chaque changement d’état public.

POUR CHAQUE PARCOURS
    Offrir navigation clavier, ordre de tabulation cohérent et focus visible.
    Donner un nom accessible à chaque bouton, champ, référence et action d’arrêt.
    À la navigation, placer le focus sur le titre ou l’action utile.
    À la fermeture d’une boîte de dialogue, restaurer le focus au déclencheur.
    Éviter les pièges de focus et prévoir sortie clavier des modales.
    Associer les erreurs au champ concerné et proposer une action de correction.
    Annoncer étapes, erreurs et fin de run avec modération,
    sans lire chaque token ni déplacer le focus pendant le streaming.
    Associer texte et icône aux états ; ne jamais utiliser la seule couleur.
    Préserver lisibilité au zoom, dans une petite fenêtre et sur longue réponse.
    Garder bouton d’arrêt/export atteignable pendant un travail.
    Le micro et la voix restent facultatifs, jamais un remplacement du clavier.

SI protocole invalide, média absent, accès interdit, file pleine ou modèle indisponible
    Montrer l’erreur réelle, son caractère réessayable et l’étape concernée.
    Ne pas remplacer l’échec par une liste vide ou une réussite.
```

## Tests d’acceptation

Parcours à vérifier dans la [matrice d’acceptation](../tests_acceptation.md), sans test applicatif effectué ici.

- [Phase 1] **T-C01, T-C02, T-C11, T-R04, T-R07, T-R09** : envoi unique, protocole, tri/déduplication, changement de conversation et sidecar absent.
- [Phase 1] **T-R11, T-R12** : message concurrent refusé sans insertion, tampon de reprise et watermark appliqués sans perdre un événement.
- [Phase 1] **T-R02, T-R05, T-F01 à T-F05** : annulation native, fermeture, import durable et erreurs ciblées.
- [Phase 2] **T-I01 à T-I03, T-I05, T-I07, T-I08** : studio sans faux chat, galerie unique, références et artefacts dédupliqués.
- [Phase 3] **T-D01 à T-D05, T-A01 à T-A06** : documents/export et voix sans perte du brouillon ni boucle micro.
- [Phase 4] **T-W01 à T-W12** : modes, cache frais, confidentialité, citations, limites et purge inspectable.
- [Phase 5] **T-M01 à T-M11** : isolation, partage, promotion, pertinence, pagination et refus des déplacements exposant un historique privé.
- [Phase 1] puis chaque phase : **T-U01 à T-U04, T-P01 à T-P12** pour accessibilité et distribution portable.

## Navigation

[Sommaire](README.md) · [Socle](backend_socle.md) · [Stockage](backend_db.md) · [Images](backend_images.md) · [Audio et documents](backend_audio_documents.md) · [Recherche](backend_recherche.md) · [Espaces](backend_espaces.md). Le [sommaire des phases](../README.md) et les [annexes](../annexes/README.md) complètent les contrats sans certifier le prototype.
