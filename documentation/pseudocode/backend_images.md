# Pseudo-code — Service image, studio et galerie

Cible conceptuelle non exécutée et non testée. Les [contrats C02, C04, C07–C11 et C13](../contrats_transversaux.md) imposent un service commun au studio et au chat, sans garantie de performance issue du seul choix d’un modèle.

## Responsabilités et interfaces

- **QUI APPELLE** : le chat via `generate_image` validé par l’exécuteur, ou le studio via une commande utilisateur directe ; tous deux invoquent le même service.
- **POURQUOI** : une seule politique de références, de ressources, de publication et d’idempotence, quel que soit le point d’entrée.
- **NE PAS CONFONDRE** : générateur texte et pipeline image, génération sans référence et édition conditionnée, galerie et historique.
- **Entrées** : prompt, paramètres de profil, `reference_ids` autorisés, portée de publication, identifiants de commande/run/action et budget.
- **Sorties** : `artifact_id` publiés, métadonnées de génération, aperçu contrôlé, progression et résultat corrélé ; aucun chemin libre ni octets d’image dans le bus.
- **Phases** : [Phase 2] activation de `generate_image`, studio direct et galerie image ; [Phase 3] même galerie étendue ; [Phase 5] partages explicites par liens.

## Un service, deux points d’entrée

```text
[Phase 2]
CONTRAT C02, C08, C09, C10, C13
QUI APPELLE : exécuteur de chat OU commande de studio depuis Rust.
POURQUOI : le studio n’a besoin ni d’une décision LLM ni d’un faux message de chat.

SI ENTRÉE PAR LE CHAT
    Recevoir un appel generate_image accepté et ses paramètres validés.
    Utiliser run_id, conversation_id et logical_action_id du tour.
    Vérifier qu’aucune location texte ne reste détenue.
    Confier la demande au service image commun.

SI ENTRÉE PAR LE STUDIO
    Valider la commande utilisateur et sa clé d’idempotence.
    Créer un Run de kind production, sans conversation ni message obligatoires.
    Garder conversation_id présent mais nul dans les événements de ce run autonome.
    Publier par ArtifactLink dans la bibliothèque personnelle locale de l’utilisateur.
    Ne pas inventer une conversation ni un message, ne pas lancer l’orchestrateur.
    Retourner l’acceptation avec run_id.
    Confier la même demande au MÊME service image et au MÊME ordonnanceur.
    Ne pas ouvrir un pipeline parallèle depuis l’UI ou Rust.

DANS LES DEUX CAS
    Passer par le registre Artifact / Blob / ArtifactLink.
    Valider paramètres et droits côté backend, même si l’UI les a contrôlés.
```

Le studio autonome utilise un run de production et la portée bibliothèque personnelle locale ; rattacher ensuite sa création à une conversation ou à un espace reste une action explicite. La galerie demeure une vue, jamais le propriétaire concurrent de fichiers. [C08, C10 et C13](../contrats_transversaux.md)

## Validation de génération et d’édition

```text
[Phase 2]
CONTRAT C10, C12, C13, C18, C19
QUI APPELLE : service image avant toute acquisition GPU.

VALIDER LA DEMANDE
    Vérifier prompt non vide et borné ; refuser les champs inconnus.
    Vérifier dimensions, ratio, nombre de sorties, étapes et réglages autorisés.
    Vérifier modèle/checkpoint, versions et capacités du profil disponible.
    Accepter de zéro à quatre références pour le profil utilisateur à certifier.
    Vérifier toujours la portée de production ; avec zéro référence,
    ne pas exiger d’artifact_id d’entrée ni en fabriquer un.
    SI le profil installé ne supporte pas la demande
        Annoncer la limite ; ne pas supprimer une référence silencieusement.
    POUR CHAQUE reference_id DANS L’ORDRE FOURNI
        Résoudre l’Artifact et vérifier son rattachement accessible.
        Vérifier Blob présent, type image réel, dimensions et décodage.
        Préparer une copie de travail et tracer ses transformations.
    Conserver l’ordre et le rôle des références ; ne pas dédupliquer
    sémantiquement deux entrées utiles seulement parce que les octets se ressemblent.
    SI la demande d’édition ne permet pas d’identifier les références
        Renvoyer un besoin de précision, pas une génération sans référence.
    Ne jamais remplacer les images d’entrée par leur seul nom dans le prompt.
```

## Flux principal de production

```text
[Phase 2]
CONTRAT C04, C07, C09, C11, C13
QUI APPELLE : service image, sans handle texte conservé.

POUR UNE DEMANDE VALIDÉE
    Consulter les sorties déjà committées pour la clé idempotente.
    SI la commande est une répétition terminée
        Revalider les droits avant de retourner ses artifact_ids, sans nouvelle diffusion.
    SINON
        Pour un studio autonome, attendre son admission dans la file bornée.
        Pour un appel chat, réutiliser l’admission lourde déjà détenue par le run parent.
        Respecter le budget d’attente et l’annulation.
        Acquérir la location exclusive de famille image.
        Faire confirmer déchargement de l’ancienne famille et nettoyage.
        Charger les composants nécessaires au pipeline dans le profil mesuré.
        Ne pas réduire ce budget à la taille du seul fichier de poids.
        Émettre run.stage décrivant chargement, encodage, diffusion ou décodage.
        Fournir prompt, paramètres et références réellement préparées au pipeline.
        Pendant le calcul, relayer les progrès observables avec fréquence bornée.
        SI étapes totales fiables
            Indiquer réalisé et total.
        SINON annoncer une étape indéterminée, pas un pourcentage inventé.
        Produire les fichiers dans la zone de préparation.
        Vérifier chaque image décodable, dimensions et type.
        Enregistrer modèle/checkpoint, versions, réglages, références, seed
        si disponible, transformations et limites de reproductibilité.
        Confirmer fin native puis libérer la location.

    POUR CHAQUE SORTIE VALIDÉE
        Utiliser run_id, logical_action_id et output_index comme clé stable.
        Publier le Blob puis Artifact et ArtifactLink selon backend_db.md.
        Ne pas garder de transaction SQL pendant la diffusion.
        Après commit, émettre artifact.created avec métadonnées bornées.
    Retourner les références publiées et les éventuelles sorties manquantes.

SI APPEL DE CHAT
    Enregistrer le résultat tool.completed dans la boucle.
    Réacquérir ensuite le texte avec une nouvelle génération de handle.
    Le générateur décrit les sorties depuis la même SharedContext et leurs IDs.
SI COMMANDE STUDIO
    Persister l’état de production puis émettre le terminal unique du run.
    Ne pas appeler le générateur texte pour clôturer le studio.
```

## Galerie et opérations sur une image

```text
[Phase 2]
CONTRAT C10, C11, C13
QUI APPELLE : vue galerie, détail d’artefact et commandes utilisateur.

POUR LISTER LA GALERIE
    Requêter les artefacts autorisés générés ou exportables de catégorie image.
    Inclure les imports seulement si le filtre explicite les demande.
    Exclure extractions internes et caches par défaut.
    Retourner une page stable de métadonnées et références de vignettes contrôlées.
    L’UI n’ouvre pas directement SQLite ni des chemins absolus.

POUR MODIFIER UNE IMAGE PAR GÉNÉRATION
    Reprendre l’artefact comme référence d’une nouvelle demande.
    Produire un nouvel Artifact ; ne pas écraser le Blob d’origine.
POUR EXPORTER OU SUPPRIMER
    Exiger une commande utilisateur distincte.
    Revalider portée et dépendances via le stockage.

[Phase 3]
    Étendre les catégories de la même galerie aux formats documentaires.
[Phase 5]
    Partager par ArtifactLink explicite, pas par copie des octets.
```

## Erreurs et arrêts

```text
[Phase 2]
CONTRAT C04, C07, C09, C11

SI modèle absent, référence interdite, format incompatible ou hors mémoire
    Retourner une erreur distincte ; ne pas substituer un modèle en silence.
SI annulation
    Demander arrêt coopératif, puis terminer le worker isolé si nécessaire.
    Attendre sa sortie avant toute location suivante.
    Conserver les images déjà publiées ; ne pas publier les sorties non validées.
    Répondre cancelled ; ne pas lancer de commentaire texte automatique.
SI plusieurs images attendues et publication incomplète
    Conserver chaque sortie committée et signaler les indices manquants.
    Distinguer résultat partial et échec total.
SI crash après publication
    Réconcilier les clés de sortie à la reprise ; pas de nouvelle diffusion aveugle.
SI nettoyage non confirmé
    Bloquer l’ordonnanceur GPU en failed, même si un aperçu a déjà été produit.
```

## Tests d’acceptation à rattacher

Scénarios associés à **T-I01 à T-I07, T-C08, T-R01 à T-R03 et T-R06** dans la [matrice de tests](../tests_acceptation.md), à exécuter.

- [Phase 2] Même demande depuis chat et studio : mêmes validations, service et stockage ; studio sans orchestrateur.
- [Phase 2] Zéro, une et quatre références, puis dépassement et référence interdite : prise en compte réelle ou refus explicite.
- [Phase 2] Texte puis image puis texte : aucune location imbriquée, ancien handle inutilisable.
- [Phase 2] Double clic, répétition transport et crash après commit : aucun doublon d’image.
- [Phase 2] Annulation pendant diffusion et pendant publication : worker réellement arrêté, sorties committées préservées.
- [Phase 2] Progression sans total fiable : état indéterminé accessible, jamais une jauge fictive.
- [Phase 2] Mesurer le profil complet et les cas multi-références ; une réussite amont ne certifie pas tous les OS/backends.
- [Phase 2] **T-I08** : studio sans conversation, conversation_id nul corrélé par run_id, puis rattachement utilisateur explicite sans faux message.

## Navigation

[Sommaire](README.md) · [Socle](backend_socle.md) · [Stockage](backend_db.md) · [Frontend](frontend.md) · [Phase 2](../phase_2_generation_image.md). Appui technique : [annexe FLUX.2 Klein](../annexes/03_flux_klein_generation.md).
