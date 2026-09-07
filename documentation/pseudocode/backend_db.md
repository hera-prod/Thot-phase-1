# Pseudo-code — Données unifiées et stockage portable

Cible conceptuelle non exécutée et non testée. Les [contrats C01, C02, C05, C06 et C09–C11](../contrats_transversaux.md) imposent une seule identité de conversation et d’artefact ; les choix de stockage dépendent aussi du [contrat de portabilité](../portabilite.md).

## Responsabilités et interfaces

- **QUI APPELLE** : commandes backend, boucle, import, services image/fichier, recherche et mémoire ; jamais le frontend directement.
- **POURQUOI** : séparer octets, identité, accès et vues sans créer une base concurrente par fonction.
- **NE PAS CONFONDRE** : `Conversation` et « session » métier, `Blob` fichier et colonne SQL binaire, `ArtifactLink` et chemin, galerie et nouvelle table de fichiers, snapshot et historique.
- **Entrées** : acteur autorisé, périmètre, identifiants stables, commande idempotente, fichier préparé ou import utilisateur, révision attendue.
- **Sorties** : objets autorisés et paginés, références de fichiers gérés, preuves, états durables et résultats de commit.
- **Phases** : [Phase 1] noyau, droits et migrations ; [Phase 2] vue galerie image ; [Phase 3] exports et galerie générale ; [Phase 4] fiches/cache ; [Phase 5] espaces/mémoire, sans table métier `Session`.

## Modèle logique et migrations

```text
[Phase 1]
CONTRAT C01, C02, C05, C06, C09, C10
QUI APPELLE : initialisation puis services de persistance.
POURQUOI : préparer des identités extensibles, pas toutes les fonctions futures.

DÉFINIR LES OBJETS DU NOYAU
    Conversation : identité, titre, ordre, révision et état du dialogue.
    Message : identité, conversation, ordre stable, rôle public, contenu et statut.
    Run de kind chat : tentative, conversation et message utilisateur obligatoires,
                       état, budgets et corrélations.
    Blob : octets immuables sur disque, chemin relatif, hash de contenu et taille.
    Artifact : identité logique, type, origine, disponibilité, Blob et métadonnées.
    ArtifactLink : rattachement de l’artefact à un périmètre autorisé.
    ToolCall : run, tool_call_id, action logique, arguments validés, résultat,
               références d’extraction, erreur, durée et état.
    Evidence : source autorisée, extrait borné, localisation et provenance.
    ContextSnapshot : conversation, révision, résumé, borne couverte et références.

NE PAS CRÉER
    Une seconde identité Source concurrente d’Artifact.
    Une table Session métier concurrente de Conversation.
    Un champ de collecte automatique du raisonnement brut.
    Une galerie contenant sa propre copie des fichiers.

POSER LES INVARIANTS
    Identifiants stables, références vérifiables et ordre de messages déterministe.
    Un message utilisateur par intention idempotente acceptée.
    Un seul run chat non terminal par conversation, garanti atomiquement à l’acceptation.
    Refuser une seconde demande concurrente sans insérer son message.
    Un résultat final par tool_call_id.
    Une publication par clé run_id, logical_action_id, output_index.
    Index de portée, jointure, ordre et hash ; contraintes d’unicité appropriées.
    Jamais un hash seul pour accorder un accès.
    Dédupliquer request_id avec empreinte canonique durable comprenant version
    de protocole, méthode, acteur/périmètre et paramètres.
    Même ID avec empreinte différente : REQUEST_CONFLICT, aucun nouvel effet.
    Revalider les droits avant de restituer une acceptation ou sortie mémorisée.
    Persister le repère seq par run avec l’état public correspondant ;
    une lecture de reprise retourne une vue cohérente et son watermark.
    Séparer sessions SQLAlchemy techniques et Conversation métier.
    Chaque unité de travail utilise sa propre session de base, sans partage
    incontrôlé entre tâches ; activer les contraintes de clés étrangères.

[Phase 2]
    Ajouter Run de kind production : conversation et message utilisateur facultatifs.
    Ajouter la portée bibliothèque personnelle locale à ArtifactLink pour le studio.
    Aucun faux message de chat n’est nécessaire à cette production autonome.
    Migrer métadonnées de génération et catégories ; exposer la galerie image.
[Phase 3]
    Étendre formats, métadonnées d’export et catégories de la même vue.
[Phase 4]
    Ajouter ArtifactCard versionnée et WebSnapshot avec fraîcheur et provenance.
[Phase 5]
    Ajouter Workspace et MemoryEntry versionnée.
    Ajouter workspace_id optionnel à Conversation.
    Étendre les portées d’ArtifactLink aux partages et mémoires autorisés.
    Ne pas déplacer d’office les anciennes conversations dans un espace.
```

Le schéma cible remplace progressivement les notions historiques du prototype via une migration explicite, et non par une coexistence indéfinie de `Source` et `Artifact`. Ce fichier ne modifie pas les modèles existants. [C01, C02 et C10](../contrats_transversaux.md)

## Ouverture et reprise du stockage

```text
[Phase 1]
CONTRAT C09, C10, C11, C19
QUI APPELLE : socle au lancement, maintenance lors d’une reprise autorisée.

OUVRIR LA RACINE DE DONNÉES
    Résoudre la racine à l’exécution depuis le mode portable choisi.
    Vérifier accès, espace disque et version de schéma.
    Préparer une sauvegarde cohérente avant migration selon politique utilisateur.
    Exécuter les migrations nécessaires dans l’ordre, sans charger de modèle.
    SI migration impossible
        Garder la sauvegarde et annoncer le blocage ; pas de remise à zéro.
    Relire les runs non terminaux ; les marquer interrupted.
    Signaler ce terminal récupéré par run.failed, code RUN_INTERRUPTED,
    lors de la resynchronisation, sans simuler une ancienne livraison.
    Ne rejouer automatiquement ni génération, ni écriture, ni requête réseau.
    Réconcilier clés d’idempotence et artefacts déjà committés.
    Signaler les références de fichiers absents comme indisponibles.
    Programmer une maintenance distincte des temporaires et blobs orphelins.
```

## Flux principal d’import et de publication

```text
[Phase 1]
CONTRAT C09, C10, C11, C18
QUI APPELLE : import utilisateur ou service producteur d’un fichier.
NE PAS CONFONDRE : publication sur disque et transaction de base de données.

POUR IMPORTER UN FICHIER UTILISATEUR
    Valider la commande, le périmètre et la référence temporaire contrôlée.
    Vérifier nom, MIME réel, taille, chemin résolu et fichier lisible.
    Copier l’original externe dans la zone de préparation gérée.
    Ne jamais transformer ni supprimer le fichier externe.
    Réutiliser la procédure commune de publication.

POUR PUBLIER UN FICHIER PRÉPARÉ
    Vérifier que la clé idempotente n’a pas déjà une sortie committée.
    Si elle existe, revalider les droits avant restitution ; aucun droit hérité du cache.
    Participer à la barrière de maintenance pour toute cette opération.
    La collecte exclusive attend cette publication, y compris la fenêtre Blob-commit.
    Écrire entièrement dans la zone de préparation ; fermer le producteur.
    Vérifier intégrité, type attendu et taille.
    Calculer le hash du CONTENU ; construire un chemin durable relatif.
    SI Blob identique dans la même racine gérée
        Réutiliser ses octets après contrôle d’intégrité.
        Ne révéler aucun lien ou métadonnée d’un autre périmètre.
    SINON publier le Blob immuable dans le stockage géré.
    Ouvrir une transaction COURTE.
        Revalider droits et annulation au point de publication.
        Créer ou retrouver Artifact selon clé idempotente.
        Créer ArtifactLink de portée explicite, provenance et métadonnées.
        Enregistrer les références de preuves et la sortie logique.
    Valider la transaction.
    Après commit ou abandon réconcilié, rendre l’accès à la barrière de maintenance.
    APRÈS COMMIT, retourner artifact_id et émettre artifact.created si lié à un run.
    Pour un import hors run, utiliser la réponse corrélée de commande,
    sans inventer un run ; un run production peut, lui, avoir conversation_id nul.
    Ne jamais tenir une transaction pendant OCR, diffusion ou rédaction.

SI ANNULATION COURT AVEC LA PUBLICATION
    Avant commit, abandonner l’enregistrement et laisser la maintenance gérer le Blob.
    Après commit, conserver l’artefact publié ; marquer le run annulé sans l’effacer.
```

## Lectures, projections et chemins

```text
[Phase 1]
CONTRAT C03, C05, C06, C10, C18
QUI APPELLE : commandes de consultation et services internes autorisés.

POUR LIRE OU EXPORTER UN ARTEFACT
    Participer à la barrière de maintenance pendant tout accès au Blob.
    Résoudre l’identité dans le périmètre explicite de l’acteur :
    conversation, ou bibliothèque personnelle à partir de la phase 2,
    ou espace autorisé à partir de la phase 5.
    Un chat n’hérite pas automatiquement du droit sur toute la bibliothèque.
    Vérifier chaque ArtifactLink utile avant de lire le Blob.
    Résoudre le chemin relatif sous la racine active.
    Refuser les traversées, liens sortants et substitutions hors racine.
    SI fichier absent, renvoyer indisponible ; ne pas fabriquer son contenu.
    Retourner métadonnées et référence de consultation contrôlée.
    Ne pas exposer le chemin absolu durable à l’UI ni au modèle.
    Garder la protection jusqu’à fermeture du lecteur/export, pas seulement
    jusqu’à la remise d’une référence de consultation à l’interface.

POUR CONSULTER LE DIALOGUE
    Lire les messages originaux ordonnés et paginés, avec leurs statuts.
    Ne pas inclure le journal technique dans Message.content.
POUR CONSULTER LE JOURNAL
    Lire actions, résultats, erreurs, provenances et durées selon rétention bornée.
    Exposer les gros corps par accès ciblé, pas dans chaque événement.
POUR CONSTRUIRE LE CONTEXTE
    Utiliser snapshot actif et suffixe selon backend_agent_loop.md.
    Ne pas modifier ni supprimer les messages couverts.

[Phase 2] [Phase 3]
POUR CONSULTER LA GALERIE
    Requêter les Artifact autorisés, générés ou exportables.
    Paginer avec ordre stable ; appliquer catégorie et filtre d’import explicite.
    Exclure caches web et extractions internes par défaut.
    Passer par le backend ; la galerie ne lit jamais SQLite directement.
```

## Liens, suppression, export et maintenance

```text
[Phase 1] [Phase 5]
CONTRAT C10, C11, C17, C19
QUI APPELLE : commande utilisateur distincte et service de maintenance.
POURQUOI : préserver les documents encore utilisés ailleurs.

POUR CHANGER LE PÉRIMÈTRE D’UN ARTEFACT
    Participer à la barrière de maintenance jusqu’au commit des liens.
    Exiger autorisation explicite ; créer ou retirer un ArtifactLink.
    Ne pas recopier les octets entre conversation et espace.
    Recalculer droits et dépendances ; ne pas transférer une source privée tacitement.

POUR DÉPLACER UNE CONVERSATION ENTRE ESPACES
    Vérifier liens, preuves, messages anciens et snapshots.
    SI indépendance vis-à-vis de l’espace d’origine non démontrée
        Refuser le déplacement, même après retrait des evidence_ids actifs.
        Proposer une nouvelle conversation avec transfert sélectif autorisé.
        Conserver originaux et provenance ; ne pas expurger l’historique.
    Sinon appliquer le déplacement autorisé selon backend_espaces.md.

POUR SUPPRIMER UNE CONVERSATION OU PURGER UN ENSEMBLE
    Présenter portée, dépendances, rétention et possibilité de sauvegarde.
    Après confirmation, retirer historique et liens choisis.
    Ne pas supprimer un Blob encore référencé par une autre conversation,
    un espace, une fiche mémoire ou une règle de rétention.
    Confier la suppression physique vérifiée à une maintenance distincte.
    L’IA ne supprime ni ne remplace directement un fichier utilisateur.

POUR EXPORTER OU SAUVEGARDER
    Construire un ensemble cohérent de base, Blob et dépendances autorisées.
    Employer des références relatives et un manifeste sans secrets.
    SI une dépendance manque ou n’est pas exportable
        Signaler précisément la limite ; ne pas promettre un paquet autonome.
    Vérifier restauration sous une autre racine avant qualification portable.

POUR TRAITER LES ORPHELINS
    Acquérir la barrière EXCLUSIVE de maintenance.
    Attendre fin des publications, lectures/exports et rattachements actifs ;
    ne pas permettre leur démarrage pendant la suppression physique.
    Attendre les producteurs de temporaires ou exclure leurs fichiers actifs.
    Seulement alors, repérer temporaires abandonnés, Blob sans commit et fichiers absents.
    Recontrôler tous les liens et la rétention APRÈS les commits des publications.
    Ne pas déduire « orphelin » de l’absence dans la seule galerie.
    Supprimer uniquement les fichiers démontrés sans dépendance ni opération active.
    Rendre la barrière à la fin, même en cas d’erreur ; ne pas bloquer l’IPC.
    Un simple délai de rétention ne remplace pas cette exclusion des courses.
```

## Erreurs et arrêts attendus

Un crash entre publication et commit peut laisser un Blob orphelin ; un commit suivi d’une coupure de transport doit rester récupérable sans nouvelle sortie. Une base pleine ou verrouillée entraîne une erreur explicite, sans terminal de réussite prématuré. Une ligne pointant vers un fichier absent reste indisponible jusqu’à récupération vérifiée. [C08–C11](../contrats_transversaux.md)

## Tests d’acceptation à rattacher

Scénarios associés à **T-C01, T-C02, T-C10, T-K02 à T-K04, T-R06, T-R08, T-F01, T-F02, T-F07, T-I05, T-I07, T-M01, T-M09 à T-M11 et T-P02 à T-P11** dans la [matrice de tests](../tests_acceptation.md), à exécuter.

- [Phase 1] Répéter une commande et une publication : même message et même sortie, résultats corrélés uniques.
- [Phase 1] Coupures avant publication, après publication et après commit : récupération distincte, aucune sortie publiée effacée.
- [Phase 1] Déplacer la racine et restaurer une sauvegarde : liens résolus, aucun chemin temporaire devenu identité durable.
- [Phase 1] Importer puis retirer le fichier externe : la copie gérée reste lisible ; l’original n’a pas été modifié.
- [Phase 1] Compaction et purge du journal : historique public original préservé et sources conservées selon dépendances.
- [Phase 2] Galerie : pas de doublon de table ni de fuite de cache interne, pagination stable.
- [Phase 5] Supprimer une conversation liée à un espace : ses liens disparaissent, les Blob encore référencés subsistent.
- [Phase 5] Duplicat de contenu dans deux espaces : aucune permission ni métadonnée révélée par le hash.
- [Phase 1] **T-R11, T-R12, T-R14** : admission atomique, état/watermark cohérents et collecte exclusive après fin des publications.

## Navigation

[Sommaire](README.md) · [Socle](backend_socle.md) · [Boucle](backend_agent_loop.md) · [Images](backend_images.md) · [Espaces](backend_espaces.md). Appui technique : [annexe Python](../annexes/06_bonnes_pratiques_python.md).
