# Pseudo-code — Espaces, liens et mémoire révisable

Cible conceptuelle non exécutée et non testée. Les [contrats C01, C02, C10, C11 et C15–C18](../contrats_transversaux.md) définissent un partage explicite, jamais un accès implicite à toutes les conversations.

## Responsabilités et interfaces

- **QUI APPELLE** : commandes utilisateur de gestion d’espace, service de recherche et service mémoire ; l’orchestrateur ne dispose pas d’écriture disque arbitraire.
- **POURQUOI** : réutiliser les ressources voulues sans perdre leur provenance ni confondre popularité et vérité.
- **NE PAS CONFONDRE** : espace et conversation, lien et copie, zone utilisateur et zone mémoire, fiche acceptée et fait certain, consultation et usage réellement retenu.
- **Entrées** : `workspace_id` optionnel de conversation, acteur, action explicite, `artifact_id`, preuves, révision de mémoire, question et curseur de pagination.
- **Sorties** : rattachements autorisés, pages de candidats pertinents, fiches versionnées, états de promotion et erreurs sans fuite inter-espaces.
- **Phases** : [Phase 5] espaces et mémoire ; réutilisation du stockage/droits de [Phase 1] et du moteur de [Phase 4]. Aucune table `Session` concurrente n’apparaît.

## Création, rattachements et frontières

```text
[Phase 5]
CONTRAT C01, C02, C10, C17, C18
QUI APPELLE : commandes utilisateur distinctes du chat.
POURQUOI : les dossiers affichés représentent des portées, pas des copies physiques.

POUR CRÉER UN ESPACE
    Vérifier la phase active et la commande autorisée.
    Créer Workspace avec identité stable et paramètres non secrets.
    Présenter deux zones logiques : fichiers utilisateur et mémoire.
    Garder les Blob dans la racine gérée ; aucun chemin absolu dans Workspace.

POUR RATTACHER UNE CONVERSATION
    Conserver son identité Conversation et son historique.
    Rendre workspace_id optionnel ; une conversation peut rester hors espace.
    Revalider les droits et les dépendances avant de changer son rattachement.
    Ne pas créer de nouvelle entité métier Session.

POUR PARTAGER UN FICHIER DANS L’ESPACE
    Exiger une commande utilisateur identifiant artefact et portée cible.
    Vérifier droit de lecture et autorisation de partager cette ressource.
    Créer un ArtifactLink de portée espace ; ne pas recopier le Blob.
    Ne pas partager automatiquement toutes les pièces jointes d’une conversation.
    Les autres conversations de l’espace voient seulement ce lien explicite.

POUR DÉTERMINER LE PÉRIMÈTRE DE LECTURE
    Inclure les pièces jointes propres à la conversation.
    Si elle possède un espace, ajouter ses seules ressources partagées autorisées.
    Exclure les pièces jointes privées des autres conversations.
    Exclure tous les autres espaces, même si un hash ou un nom est identique.
```

## Flux principal de consultation et de classement

```text
[Phase 5]
CONTRAT C15, C16, C17
QUI APPELLE : cascade de recherche de backend_recherche.md.
NE PAS CONFONDRE : classement contextuel et top cinq permanent.

APRÈS CONTEXTE, PIÈCES JOINTES ET CACHE WEB ADMISSIBLE DE CONVERSATION
    Chercher d’abord dans les fichiers utilisateur partagés de l’espace.
    Évaluer preuves, couverture, fraîcheur et contradictions.
    SI suffisant, retourner sans réseau.
    Sinon chercher dans la mémoire du même espace.

POUR CHAQUE RECHERCHE MÉMOIRE
    Filtrer d’abord les droits et les sources encore accessibles.
    Écarter les fiches rejected de l’usage ordinaire.
    Ne pas traiter candidate comme mémoire acceptée.
    Signaler stale ; ne pas utiliser sa synthèse comme fait actuel confirmé.
    Filtrer puis classer les candidats selon pertinence pour la question.
    Utiliser fréquence, récence et retour explicite seulement comme signaux secondaires.
    Stabiliser les égalités par un critère déterministe.
    Retourner une page avec curseur, révision de classement et raisons structurées.
    Une taille initiale de cinq reste une taille de page, pas une limite totale.
    Permettre page suivante, recherche lexicale et ouverture ciblée par identifiant.
    SI révision du classement change entre deux pages
        Invalider le curseur ou refaire la requête explicitement ;
        ne pas masquer des résultats par décalage silencieux.
    Ouvrir les preuves nécessaires derrière les fiches sélectionnées.
    Ne jamais transformer une synthèse en source primaire par répétition.
    SI rien d’exploitable dans la première page
        Élargir la recherche locale dans le budget, pas conclure à l’absence.

À LA FIN DE LA RECHERCHE LOCALE
    Revenir à la même cascade et au même budget.
    Respecter local_only ; réseau simple puis approfondi seulement si autorisés.
```

## Promotion mémoire explicite et versionnée

```text
[Phase 5]
CONTRAT C09, C10, C11, C16, C17
QUI APPELLE : proposition du service mémoire, validation par l’utilisateur.
POURQUOI : une réponse utile ne devient pas une mémoire partagée en secret.

POUR PROPOSER UNE FICHE
    Recevoir contenu synthétique, sources, contexte d’origine et raisons structurées.
    Vérifier que les sources sont accessibles et localisables.
    Vérifier date, périmètre cible, droits de partage et limites de fraîcheur.
    Vérifier la clé idempotente de proposition.
    Créer MemoryEntry versionnée au statut candidate, avec références de preuves.
    Stocker par le service mémoire ; pas de chemin libre transmis au LLM.
    Rendre la proposition visible et modifiable par l’utilisateur.
    Ne pas l’exposer comme mémoire accepted à toutes les conversations.

SI L’UTILISATEUR VALIDE
    Revalider droits, révision attendue et sources au moment de la confirmation.
    Vérifier si les dépendances privées de la conversation doivent être partagées.
    SI elles ne sont pas accessibles depuis le périmètre cible
        Demander un partage explicite des dépendances ou refuser la promotion.
        Ne pas transférer ces sources silencieusement.
    Sinon enregistrer accepted, date, auteur de validation et version.
    Créer les rattachements mémoire appropriés dans une transaction courte.
    Enregistrer l’action de promotion de façon idempotente.

SI L’UTILISATEUR REFUSE
    Passer à rejected avec motif facultatif et sans publication partagée.
SI source devenue indisponible, périmée ou contredite
    Marquer stale ; conserver provenance et statut révisable.
SI révision concurrente au moment de valider
    Refuser le conflit ; relire la version courante avant une autre confirmation.

UNE AUTOMATISATION ULTÉRIEURE
    Reste optionnelle et auditable ; elle n’est pas activée dans ce premier jalon.
```

## Usage, correction et zone utilisateur

```text
[Phase 5]
CONTRAT C11, C16, C17
QUI APPELLE : fin de réponse validée, puis commandes utilisateur de révision.

POUR METTRE À JOUR L’USAGE
    Vérifier que la preuve liée à la fiche a réellement été retenue dans la réponse.
    Dédupliquer l’usage par réponse et révision de fiche.
    Mettre à jour fréquence et récence de cet usage réel.
    Enregistrer les retours explicites séparément de la popularité.
    Ne pas incrémenter lors de simple ouverture, recherche ou affichage.
    Ne pas interpréter un compteur élevé comme validation de vérité.

POUR CORRIGER, INVALIDER OU FUSIONNER
    Montrer contenu, sources, versions et contradictions à l’utilisateur.
    Vérifier la révision attendue.
    Produire une nouvelle version ou un statut révisé, avec provenance.
    Une fusion conserve les références des fiches d’origine et les désaccords utiles.
    Propager stale aux dépendances affectées plutôt que réécrire le passé.

POUR MODIFIER UN DOCUMENT DE LA ZONE UTILISATEUR
    Refuser une écriture demandée directement par l’IA.
    Une proposition de modification est un NOUVEL Artifact.
    Seule une commande utilisateur distincte peut accepter un remplacement.
    Revalider droits et confirmation de destination via le stockage.
```

## Déplacement, export, purge et erreurs

```text
[Phase 5]
CONTRAT C09, C10, C11, C17, C18, C19
QUI APPELLE : utilisateur, jamais un effet implicite de recherche.

POUR DÉPLACER UNE CONVERSATION VERS UN AUTRE ESPACE
    Sérialiser l’opération avec les runs utilisant son périmètre.
    Afficher les liens et dépendances qui cesseraient d’être accessibles.
    Ne pas transférer mémoire ou sources privées de l’espace d’origine.
    Inspecter aussi les anciens messages et snapshots pouvant contenir des extraits.
    Retirer des evidence_ids ne suffit pas à retirer ces informations du texte.
    SI indépendance vis-à-vis des ressources de l’ancien espace non démontrée
        Refuser le déplacement dans le profil initial.
        Proposer une nouvelle conversation destination, puis une sélection explicite
        et autorisée de textes/artefacts à y transférer avec provenance.
        Préserver la conversation originale et ses snapshots intacts.
        Ne pas expurger automatiquement des passages pour rendre le déplacement possible.
        ARRÊTER avant toute modification du rattachement.
    Après confirmation, changer le rattachement et réviser le contexte.
    Invalider catalogues et curseurs devenus périmés.
    Conserver les références historiques avec un signalement des accès perdus.

POUR EXPORTER UN ESPACE OU UN SOUS-ENSEMBLE
    Résoudre les dépendances autorisées des conversations, fiches et artefacts.
    Inclure les Blob nécessaires sous références relatives.
    Signaler les ressources absentes ou exclues ; ne pas promettre une copie autonome.

POUR PURGER LA MÉMOIRE OU SUPPRIMER L’ESPACE
    Exiger choix utilisateur, confirmation, dépendances et option de sauvegarde.
    Retirer seulement les liens et fiches sélectionnés.
    Préserver les documents utilisateur et Blob encore référencés.
    Laisser la maintenance distincte décider des suppressions physiques sûres.

SI droit insuffisant, source manquante, conflit de révision ou espace supprimé
    Retourner une erreur sans nom ni aperçu de ressources interdites.
    Conserver l’ancien état committé ; aucune promotion partiellement visible.
SI annulation ou crash pendant une opération
    Réconcilier clé idempotente, versions et liens ; ne pas recommencer aveuglément.
```

## Tests d’acceptation

Ces scénarios sont à exécuter selon la [matrice d’acceptation](../tests_acceptation.md), sans réussite présumée.

- [Phase 5] **T-M01, T-M02** : deux espaces, noms identiques et conversation hors espace ; isolation, partage choisi et absence de recopie.
- [Phase 5] **T-M03, T-M04** : IA refusée en écriture utilisateur ; promotion candidate puis acceptée/refusée, jamais cachée.
- [Phase 5] **T-M05, T-M06** : fiche pertinente devant une fiche populaire hors sujet, preuve accessible après la cinquième place.
- [Phase 5] **T-M07** : dix consultations sans usage effectif ne gonflent pas le compteur d’utilité.
- [Phase 5] **T-M08** : contradiction et ancienneté visibles ; correction et invalidation conservent les sources.
- [Phase 5] **T-M09** : déplacement et purge recalculent les droits sans transférer les sources privées.
- [Phase 5] **T-M11** : ancien message contenant un extrait privé, même sans preuve active ; déplacement refusé si indépendance non démontrée, transfert sélectif explicite proposé.
- [Phase 5] **T-M10**, avec **T-P02** : export et restauration dans une autre racine, dépendances incluses ou manques explicités.

## Navigation

[Sommaire](README.md) · [Stockage](backend_db.md) · [Recherche](backend_recherche.md) · [Frontend](frontend.md) · [Phase 5](../phase_5_espaces_et_memoire.md). Appui technique : [annexe recherche et mémoire](../annexes/07_recherche_agents_memoire.md).
