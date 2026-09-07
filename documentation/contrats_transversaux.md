# Contrats transversaux de Thot

Ce document définit les règles de conception communes aux cinq phases. Il décrit une cible à implémenter, pas le comportement garanti du prototype présent dans le dépôt ; les [tests d’acceptation](tests_acceptation.md) serviront à vérifier cette cible.

Une règle identifiée `Cxx` est normative pour la préproduction. Les chiffres proposés comme paramètres sont des valeurs de départ à mesurer, non des résultats de benchmark.

## C01 : vocabulaire et ordre de vérité

- **Conversation** : discussion persistante ; « session » est seulement un synonyme métier, pas une deuxième entité concurrente.
- **Tour** : message utilisateur, traitement associé et réponse éventuelle. Un `run_id` identifie une tentative d’exécution ; une nouvelle tentative ne duplique pas le message utilisateur.
- **Orchestrateur** : rôle LLM qui choisit des actions permises et prépare un brief ; il ne possède pas les droits du système d’exploitation.
- **Exécuteur** : logiciel déterministe qui valide puis exécute les actions. Ce n’est pas le générateur et ce n’est pas un second agent autonome.
- **Générateur** : rôle du même modèle texte, sans outils, qui rédige depuis le contexte partagé et les observations sélectionnées.
- **Artefact** : ressource adressable, importée ou produite, avec identité, métadonnées et droits. `Blob` désigne ses octets ; `ArtifactLink` désigne son rattachement à un périmètre.
- **Preuve (`Evidence`)** : extrait ou observation localisable avec provenance ; ni un résumé, ni une réponse antérieure ne deviennent une source primaire par répétition.
- **Snapshot** : projection versionnée du contexte utilisé pour l’inférence ; il n’est pas l’historique original.

En cas de contradiction, appliquer dans cet ordre : exigence utilisateur explicite, présent contrat, document de phase, pseudo-code. Consigner l’écart et corriger le document inférieur ; ne pas conserver deux règles incompatibles. Les annexes étayent les faits techniques, elles ne décident pas seules du produit.

## C02 : activation par phase

| Phase | Capacités actives | Réservations, non fonctionnalités anticipées |
| :--- | :--- | :--- |
| 1 | Chat, historique, imports image/PDF/texte, catalogue, lecture, vision/OCR, compaction, stockage et packaging portable initial | Artefacts génériques, schémas versionnés, liens de portée conversation |
| 2 | Studio image, `generate_image`, galerie image, références jusqu’à 4 pour le profil utilisateur à certifier | Catégories de galerie extensibles |
| 3 | TXT/Markdown/CSV/PDF générés, export, galerie générale, dictée et lecture vocale | Contrats audio indépendants des outils LLM |
| 4 | Fiches sémantiques, recherche documentaire, cache web, recherche simple et approfondie | Preuves et politique de fraîcheur |
| 5 | Espaces, fichiers partagés utilisateur, mémoire révisable et classement contextualisé | Aucun accès global implicite entre espaces |

Les droits, identifiants stables, erreurs, tests et chemins portables commencent en phase 1. Ne pas développer dès cette phase toutes les tables ou vues futures ; employer des migrations explicites quand une capacité apparaît.

## C03 : identité du contexte et séparation des rôles

Pour un tour, construire une seule base `SharedContext(conversation_id, revision)` composée du résumé actif éventuel, des échanges récents ordonnés, du message utilisateur courant exactement une fois et d’un catalogue compact des ressources accessibles. Le résumé pointe vers sa borne d’historique ; les messages ne peuvent pas être présents à la fois dans le suffixe et comme messages déjà couverts.

L’orchestrateur et le générateur reçoivent la même révision de cette base, non deux historiques construits indépendamment. Ce qui diffère est le prompt système, les capacités, et les ajouts propres au tour :

| Destinataire | Ajouts autorisés à la base | Contenu exclu |
| :--- | :--- | :--- |
| Orchestrateur | Schémas d’outils autorisés, séquence structurée des appels/résultats du tour courant, budget restant | Traces de raisonnement anciennes, ressources hors périmètre |
| Générateur | Brief contractuel, observations sélectionnées et localisées, références des artefacts produits, limites rencontrées | Chaîne de pensée, monologue de planification, transcript brut de l’orchestrateur, définitions d’outils |
| Compacteur | Historique public à résumer, résumé précédent si nécessaire, références durables, consigne de compaction | Raisonnement privé et permission d’appeler des outils |

Le brief est un objet contraint : `user_goal`, `response_constraints`, `evidence_ids`, `artifact_ids`, `unresolved_points`, `execution_status`. Aucun champ `thinking`, `reasoning`, ou verbatim de réponse interne n’y est accepté. Les contraintes ne peuvent pas contredire celles de l’utilisateur ; les identifiants sont résolus et revalidés par le backend.

Une chaîne libre intitulée « brief » n’est pas une protection suffisante. Contrôler les champs, la taille et la provenance ; filtrer toute narration de planification détectée et, si le brief est invalide, reconstruire un brief minimal depuis la demande originale et les résultats validés.

Ne pas demander au modèle d’exposer sa chaîne de pensée. Les états visibles sont des descriptions d’action comme « Lecture du document », pas la transcription de son raisonnement.

## C04 : boucle outils, retour et arrêt

Chaque appel valide produit exactement un résultat corrélé par `tool_call_id`, y compris en cas d’échec. Ajouter la requête structurée et le résultat à la séquence du tour avant toute nouvelle décision ; accumuler les preuves et artefacts hors de la boucle, sans les réinitialiser à chaque itération.

Après un appel vision ou image, le handle LLM précédent est invalide : réacquérir le runtime texte et reconstruire le contexte depuis les données persistées ou la projection du tour. Ne jamais poursuivre sur un objet dont le modèle a été déchargé.

Un arrêt peut résulter d’un brief valide, d’un budget épuisé, d’un doublon sans progrès, d’une annulation ou d’une erreur non récupérable. En cas de limite, le générateur peut produire une réponse partielle signalée ; en cas d’annulation, ne pas lancer automatiquement une nouvelle génération.

Les budgets incluent nombre de décisions, appels, tokens, volume de résultats et durée murale. Proposer initialement cinq décisions pour le chat simple est une configuration modifiable ; un délai fixe de quinze secondes n’est pas présumé adapté à l’OCR d’un PDF ou à la diffusion.

Réserver une enveloppe de finalisation comptée dans le budget du run : sans réserve restante, émettre un message de limite déterministe plutôt qu’un nouvel appel LLM. La réserve de sortie du modèle ne réserve pas automatiquement la durée ni les tokens du run.

## C05 : historique durable et contexte d’inférence

- **Historique utilisateur** : messages et artefacts affichés, avec statut `complete`, `partial`, `cancelled` ou `failed`. Une réponse interrompue n’est pas présentée comme achevée.
- **Journal technique** : actions structurées, arguments validés, résultats/provenances, erreurs et durées. Persisté de façon bornée pour diagnostic et reprise, mais non réinjecté intégralement dans les tours futurs.
- **Raisonnement privé brut** : non collecté par défaut, non stocké dans `Message.content` et jamais transmis au générateur.
- **Corps volumineux** : stocké comme extraction ou artefact interne, avec aperçu borné ; l’IPC ne transporte pas tout un document dans un événement de résultat.

Les deux « historiques » souhaités deviennent deux projections d’un stockage cohérent : dialogue public et journal d’exécution. « Ne pas le mettre en contexte » ne signifie donc pas « supprimer toutes les observations ».

Le décodage du template doit séparer canaux et appels structurés avant l’affichage. Une expression régulière supprimant arbitrairement du français ou tout texte entre crochets n’est pas une garantie de séparation.

## C06 : compaction non destructive et budget réel

Conserver les messages originaux. Produire un `ContextSnapshot` versionné contenant résumé, `covered_through_message_id`, révision et références nécessaires ; n’activer le nouveau snapshot que si sa validation réussit et si la révision de la conversation n’a pas changé.

La cible produit reste le grand contexte annoncé du modèle, avec essai utilisateur à reproduire. Pour chaque rôle, compter avec le tokenizer et le template effectifs :

`tokens du système + base partagée + schémas/outils + observations + réserve de sortie + marge <= contexte configuré et supporté`.

Le seuil envisagé autour de 110 000 jetons est un signal préventif, pas une constante universelle. Déclencher plus tôt si le prompt du rôle suivant ne tient pas ; si le résumé échoue, conserver l’ancien snapshot et afficher une erreur ou proposer un profil réduit, jamais tronquer silencieusement.

Un résumé trop long ou un historique déjà hors fenêtre est traité par segments bornés puis synthèse, avec préservation des références et incertitudes. La compaction est sérialisée avec le tour pour éviter qu’un nouveau message disparaisse.

Le snapshot ne garantit pas une mémoire parfaite. Garder un accès aux messages archivés et aux sources pour les demandes exactes, citations et nombres critiques.

Préparer la compaction avant de figer `SharedContext` et réserver un budget borné pour les observations du tour. Si un résultat arrive trop volumineux, conserver le brut hors prompt et sélectionner des extraits localisés ; ne pas compacter seulement le contexte du générateur en lui donnant une révision différente. Si le budget reste impossible, arrêter avec une erreur de contexte et préparer un nouveau tour/projection de façon explicite.

## C07 : ressources, runtimes et concurrence

Un ordonnanceur applicatif contrôle tous les travaux d’inférence. Le profil 8 Go ne donne qu’une location exclusive du GPU à une famille de pipeline à la fois : texte, vision/OCR, ou image ; un pipeline peut inclure plusieurs composants nécessaires et leur chargement interne doit lui-même respecter le profil mesuré.

L’admission d’un run lourd est acquise une seule fois avant son traitement et rendue après arrêt confirmé. Les outils, la compaction et les sous-recherches de ce run réutilisent cette admission ; seule la location de runtime est acquise/libérée par sous-opération. Ne pas attendre un deuxième emplacement de run depuis un run qui occupe déjà l’unique place.

États attendus : `unloaded`, `loading`, `ready`, `running`, `unloading`, `failed`. Enregistrer le propriétaire, la génération de handle et l’état de nettoyage ; un vieux handle ne redevient pas valide parce que le même modèle est rechargé.

La mesure du budget est spécifique au backend. VRAM dédiée, mémoire unifiée, RAM, espace disque et fichiers mappés ne sont pas interchangeables ; un détecteur NVIDIA seul ne certifie pas AMD, Apple ou CPU.

La libération doit être confirmée par le runtime et les observations disponibles, avec délai borné. Si la fermeture d’une bibliothèque native échoue, terminer son worker isolé et attendre sa sortie avant d’autoriser un autre chargement ; aucune relance en parallèle pour « essayer quand même ».

La cible est un coordinateur Python non bloquant avec workers d’inférence isolables, choix à prototyper en phase 1. Un seul run lourd actif pour tout le profil portable simplifie le début ; les autres conversations restent consultables et leurs demandes sont mises en file bornée. L’audio CPU de phase 3 dispose de quotas CPU/RAM pour ne pas affamer l’UI ou l’inférence CPU.

Les replis sont explicites : backend indisponible, hors mémoire, modèle absent et format incompatible sont des erreurs distinctes. Ne jamais retirer le projecteur d’un modèle de vision ni substituer un handler d’une autre architecture pour masquer une erreur.

## C08 : IPC et état de l’interface

Le pont reste frontend → commandes Tauri/Rust → sidecar Python en JSON Lines pour le contrôle. Une ligne contient un objet JSON UTF-8 ; logs sur stderr, sérialiseur de sortie unique et flush contrôlé.

Contrat logique versionné :

| Enveloppe | Champs requis |
| :--- | :--- |
| Requête | `protocol_version`, `request_id`, `method`, `params` |
| Réponse de commande | `request_id`, `ok`, `result` ou `error` |
| Événement de run | `protocol_version`, `event`, `run_id`, `conversation_id`, `seq`, `payload` |
| Erreur | `code`, `message`, `retryable`, `details` sans secrets ni chemin sensible inutile |

`chat.send` retourne une acceptation et le `run_id`, pas la réponse finale. Le stream est ensuite corrélé au run ; un événement terminal unique `run.completed`, `run.cancelled` ou `run.failed` clôt ce run, après persistance de son état.

`Run.kind=chat` exige `conversation_id` et `user_message_id` ; `Run.kind=production` permet ces valeurs nulles pour le studio autonome. Le champ `conversation_id` reste présent dans les événements mais peut donc valoir `null` ; `run_id` et `seq` assurent toujours leur corrélation. L’unicité terminale est logique : une coupure peut empêcher sa livraison, et une retransmission peut être dédupliquée ; la consultation durable est l’autorité de reprise.

Les états de run sont `queued`, `running`, `cancelling`, `completed`, `cancelled`, `failed` et `interrupted`. Après crash, `interrupted` est un état terminal récupéré ; la resynchronisation peut l’annoncer comme `run.failed` avec code `RUN_INTERRUPTED`, sans prétendre qu’un ancien événement de fin a été livré.

Noms canoniques de conception : `backend.ready`, `run.started`, `run.stage`, `tool.started`, `tool.completed`, `answer.delta`, `artifact.created`, puis événement terminal. Les noms historiques du prototype (`answer`, `message_completed`, etc.) demandent un adaptateur versionné, pas une compatibilité présumée.

`seq` croît par run, permet de rejeter doublons et événements périmés. Un delta tardif d’une ancienne conversation ne doit pas apparaître dans la nouvelle ; une déconnexion déclenche resynchronisation depuis la base via le backend.

Le lecteur stdin et le writer stdout restent disponibles pendant l’inférence. Annulation, statuts et fermeture doivent être reçus même si un outil est lent. Bornes de messages, taille de file et backpressure sont configurées ; files pleines donnent une erreur, pas une croissance RAM non bornée.

L’audio et les médias volumineux utilisent des références de fichiers temporaires contrôlés, ou un canal binaire dédié ultérieur. Pas d’octets audio arbitraires dans du JSON et pas de base64 d’images dans l’historique texte ; un adaptateur multimodal peut encoder une image pour son API propre, hors du bus de contrôle.

La resynchronisation lit un état durable avec watermark : les événements arrivés pendant la lecture sont tamponnés, puis seuls ceux qui suivent le watermark sont appliqués. `seq` ou époque de backend est persisté pour reprendre la séquence après redémarrage ; un compteur périmé ne fait jamais ignorer un run récupéré `interrupted`.

## C09 : annulation, reprise et idempotence

L’annulation est d’abord coopérative, puis escaladée au worker isolé après un délai configurable. Fermer une coroutine ou cesser d’attendre ne suffit pas comme preuve que le calcul natif a cessé.

Les effets visibles sont indexés par une clé idempotente, par exemple `(run_id, logical_action_id, output_index)`. Une relance du transport ne crée pas deux messages, deux images ni deux fiches mémoire.

Après crash, marquer les runs non terminaux `interrupted` à la reprise et proposer la relance. Ne pas rejouer automatiquement une écriture ou un appel réseau dont l’achèvement est incertain ; réconcilier d’abord les artefacts déjà enregistrés.

Une conversation admet au plus un run de chat non terminal à la fois, garanti de façon atomique à l’acceptation ; une seconde demande est refusée sans insérer son message. La file des autres conversations reste permise.

Les écritures en préparation peuvent laisser des temporaires ou blobs orphelins. Une procédure de récupération séparée les traite sans effacer une sortie publiée ou un document utilisateur.

## C10 : stockage, identifiants et droits

Chemins durables relatifs à une racine de données résolue au lancement. Aucun chemin absolu de développeur, nom de lettre de lecteur ou référence à un répertoire temporaire dans les identifiants durables.

Structure logique cible à migrer progressivement :

| Objet | Introduit | Responsabilité |
| :--- | :--- | :--- |
| Conversation, Message, Run | 1 | Dialogue, exécution, état et révisions |
| Blob | 1 | Octets immuables, chemin relatif, hash contenu, taille |
| Artifact, ArtifactLink | 1 | Identité logique, origine/type et accès conversation ; galerie en phase 2 |
| ToolCall, Evidence | 1 | Actions et observations localisables, sans raisonnement privé |
| ContextSnapshot | 1 | Projection compacte, borne d’historique et version |
| ArtifactCard, WebSnapshot | 4 | Description indexée, URL et date de récupération |
| Workspace, MemoryEntry | 5 | Partage cloisonné, synthèses et révisions |

Une galerie est une vue d’artefacts générés/exportables, pas une table dupliquant `Source`. Les imports peuvent y apparaître via un filtre explicite ; caches et extractions internes restent hors de la galerie par défaut.

En phase 2, `ArtifactLink` admet aussi une portée de bibliothèque personnelle locale pour le studio sans conversation. Cette portée explicite ne donne pas à toutes les conversations le droit de lire ses fichiers : une sélection/importation par l’utilisateur crée le rattachement autorisé. Les portées d’espace n’apparaissent qu’en phase 5.

Le modèle ne fournit que `artifact_id`, `evidence_id` et sélecteurs bornés (page, plage, question). Le backend vérifie les rattachements accessibles à chaque lecture, export et écriture ; un hash connu ne constitue jamais une autorisation.

La validation commune vérifie toujours la portée, mais ne résout que les références présentes ou exigées par le schéma de l’outil ; un catalogue ou une génération sans référence n’est pas forcé de produire un `artifact_id`.

À l’import, copier dans le stockage géré après validation, puis travailler sur cette copie. Les originaux externes restent inchangés ; un mode de référence externe, s’il est ajouté ultérieurement, est explicitement non autonome et exclu de la promesse portable stricte.

Les changements de portée se font par rattachement, pas par recopie entre conversations et espaces. La déduplication physique est limitée à une même racine gérée et ne révèle pas l’existence d’une ressource d’un autre espace.

## C11 : publication de fichiers et suppression

La base et le système de fichiers ne partagent pas une transaction unique. Procédure cible : écrire dans une zone de préparation, vérifier le fichier, calculer son empreinte, publier le blob, puis enregistrer artefact/rattachement dans une transaction courte. Émettre `artifact.created` après commit ; ne pas tenir une transaction ouverte pendant une inférence.

La collecte physique s’exécute sous une barrière de maintenance : aucune publication, lecture/export ou nouveau rattachement concurrent pendant la suppression. Un recontrôle de liens ne doit pas précéder le commit d’une publication encore active ; les réservations par blob d’une implémentation ultérieure devront protéger la fenêtre publication-commit.

Si un crash survient entre publication du blob et commit, le blob est orphelin et récupérable par maintenance ; si une ligne référence un fichier absent, marquer la ressource indisponible, ne pas inventer un contenu.

Supprimer une conversation retire ses liens et son historique selon choix utilisateur, pas les blobs encore référencés par un espace. La suppression physique tient compte de tous les liens, références de mémoire et règles de rétention, avec confirmation et possibilité de sauvegarde.

L’utilisateur contrôle suppression, export et purge ; l’IA ne supprime ni ne remplace directement ses fichiers. En phase 5, une modification de document utilisateur exige une action utilisateur distincte ; une révision proposée par l’IA est un nouvel artefact.

La déduplication de transport utilise aussi `request_id` et l’empreinte des paramètres : même identifiant et mêmes paramètres retrouvent l’acceptation existante ; même identifiant avec paramètres différents produit `REQUEST_CONFLICT`. Après redémarrage, ces clés se résolvent dans le stockage durable, pas seulement dans une mémoire de processus.

L’empreinte canonique comprend la version de protocole, la méthode, l’acteur/périmètre et les paramètres ; mêmes paramètres mais méthode différente ne retrouve pas une autre acceptation. Revalider les droits avant de restituer un résultat d’idempotence mémorisé.

## C12 : catalogue, lecture et extraction

Phase 1 : catalogue minimal avec ID, nom, type détecté, provenance, disponibilité et ordre de dépôt. Les descriptions sémantiques riches et recommandations apparaissent en phase 4, versionnées selon contenu/extracteur.

Un résumé de catalogue sert au routage, pas à affirmer qu’un fichier contient la réponse. Une ambiguïté comme « cette image » parmi plusieurs références non distinguées déclenche une clarification, jamais le choix silencieux de la dernière.

Lire le texte exploitable d’un PDF avant d’OCRiser les pages nécessaires ; un besoin de mise en page ou de tableau peut justifier le rendu même si du texte existe. Traiter pages et images en lots bornés, conserver numéro de page, sélection et transformations ; ne pas charger toutes les pages rendues en RAM.

OCR et vision produisent des observations potentiellement erronées. Signaler incertitude ou illisibilité ; la validation de format ne transforme pas une observation en certitude.

## C13 : outils de production de contenu

Phase 2 : `generate_image` accepte prompt, paramètres validés et références autorisées. Le studio direct et le chat passent par le même service d’image, registre d’artefacts et ordonnanceur ; seul le chat passe par l’orchestrateur.

Le studio peut produire dans la bibliothèque personnelle sans créer de conversation ni de message artificiel. Son run de production possède son propre périmètre autorisé ; attacher ensuite la création à une conversation ou un espace exige un rattachement explicite et une vérification des droits.

Un artefact produit porte modèle/checkpoint, versions, réglages, références d’entrée, seed si disponible et limites connues. La reproductibilité exacte inter-backends est un objectif à évaluer, pas une promesse.

Phase 3 : `generate_file` accepte une union de schémas `txt`, `markdown`, `csv`, `pdf`, et non un programme arbitraire. Autoriser tailles et styles prédéfinis ; ne pas exécuter du Python, SQL, HTML actif ou shell rédigé par le LLM.

Valider contenu, références et syntaxe avant publication. Un PDF ouvrable n’implique pas des chiffres justes ; distinguer validité technique, fidélité aux données et exactitude factuelle.

## C14 : audio utilisateur

La dictée est une entrée utilisateur modifiable, pas un outil que l’orchestrateur déclenche de lui-même. Le premier jalon est appuyer/parler/arrêter, transcription finale puis envoi manuel ; la transcription continue reste une amélioration conditionnée aux mesures.

Un `recording_id` et la révision du brouillon évitent d’écraser un texte modifié pendant la transcription. Permission refusée, périphérique absent, silence, bruit et échec du modèle sont des cas prévus.

La lecture vocale porte sur un message choisi, ne lit pas les traces internes et peut être arrêtée. Nettoyer le balisage de façon prévisible sans changer les faits ; offrir choix de langue/voix, volume et vitesse selon moteur.

Audio temporaire supprimé selon politique visible, conservation sur demande. Lecture et micro simultanés doivent empêcher une boucle de réenregistrement, par suspension du micro au départ si nécessaire.

## C15 : politique de recherche et suffisance

La cascade est une politique Thot, non une garantie scientifique : contexte → pièces jointes de conversation → cache web de conversation admissible →, en phase 5, fichiers utilisateur puis mémoire de l’espace → réseau simple → recherche approfondie.

L’ordre local peut regrouper les sources sans multiplication coûteuse des appels. Toujours contrôler portée, pertinence, couverture des sous-questions, contradictions et fraîcheur avant de juger les éléments suffisants ; un top-k vide ou une mauvaise extraction n’est pas la preuve que le document ne contient rien.

Modes explicites : `local_only`, `web_allowed`, `deep_research`. Une demande « d’après ce PDF uniquement » interdit le réseau ; une demande « vérifie le prix aujourd’hui » peut exiger une revalidation réseau, sans imposer la relecture de documents non pertinents.

Ne pas envoyer automatiquement un extrait privé ou des données personnelles au moteur distant. Reformuler une requête minimale et demander l’accord avant un transfert sensible ; montrer le périmètre et le mode choisis.

Un budget global borné limite requêtes, pages lues, profondeur, temps, taille et tokens. La recherche approfondie réalise des itérations jusqu’à couverture suffisante ou limite, avec détection de doublons et rapport des lacunes ; elle n’est pas une recherche « infinie ».

## C16 : cache, preuves et citations

Le cache stocke des snapshots : URL demandée/finale, date de récupération, date de publication si connue, statut HTTP, validateurs disponibles, hash du contenu, texte extrait, version d’extracteur et portée autorisée. Un hash d’URL ne remplace pas le hash du contenu.

Réutilisation si le sujet, la portée et la fraîcheur conviennent ; revalidation selon type de fait, cache HTTP disponible, requête utilisateur et politique locale. Ne jamais promettre « aucune URL n’est visitée deux fois ».

Les pages sont des données non fiables, pas des instructions. Les résumés gardent leurs sources et limites ; l’orchestrateur ne peut pas les promouvoir en consigne système.

Un fait cité conserve `evidence_id`, source (URL ou artefact), extrait et localisation. Le générateur reçoit un ensemble autorisé ; le renderer/validateur refuse les références inventées et rend les liens sans HTML actif.

Les snapshots sont absents de la galerie publique par défaut, mais inspectables et purgeables par l’utilisateur depuis une vue « sources/cache ». « Interne » ne signifie pas caché à son propriétaire.

## C17 : espaces et mémoire

Une conversation peut rester hors espace ; `workspace_id` est optionnel pour ne pas forcer la migration de tout l’historique. Pour une conversation dans un espace, seules ses pièces jointes et les ressources partagées de cet espace sont accessibles.

La zone utilisateur est en lecture seule pour l’IA, modifiable par l’utilisateur via des commandes distinctes. La zone mémoire autorise des fiches versionnées par le service mémoire, jamais une écriture arbitraire sur le disque.

Une fiche promue en mémoire nécessite sources accessibles, contexte d’origine, date, périmètre, statut (`candidate`, `accepted`, `stale`, `rejected`) et raisons structurées de promotion. En phase 5 initiale, l’utilisateur valide la promotion ; une automatisation ultérieure sera optionnelle et auditable.

La pertinence de requête et les droits filtrent les candidats avant le classement. Score de fréquence/récence/retour explicite seulement comme signal secondaire ; incrémenter l’usage lorsque la preuve a réellement été retenue, pas à chaque consultation. Un score élevé n’est pas une validation de vérité.

`k=5` peut être une taille de page initiale, jamais une limite empêchant toute recherche au-delà des cinq fiches populaires. Conserver pagination, recherche lexicale et ouverture ciblée ; pouvoir corriger, invalider, fusionner et supprimer les fiches.

Déplacer une conversation entre espaces exige de recalculer ses droits et références : ne pas transférer tacitement la mémoire/source privée de l’espace d’origine. Exporter un sous-ensemble requiert les ressources dont il dépend ou un signalement explicite de manque.

Retirer des `evidence_ids` ne suffit pas : les anciens messages et snapshots peuvent déjà contenir des extraits privés. Dans le profil initial, refuser le déplacement si l’indépendance vis-à-vis des ressources de l’espace d’origine ne peut pas être démontrée ; proposer une nouvelle conversation dans la destination, puis une sélection explicite et autorisée de textes/artefacts à recopier. Conserver l’original intact et sa provenance ; un transfert plus fin avec revue de divulgation est une extension, pas une suppression automatique de morceaux d’historique.

## C18 : sécurité, confidentialité et accessibilité

Le backend constitue la frontière d’autorisation. Contrôler noms, MIME réel, tailles, budgets, chemins résolus, liens symboliques/jonctions et portée ; ne pas employer un simple préfixe de chaîne comme garde de chemin.

En phase 4, le fetcher interdit les accès réseau internes non autorisés, schémas inattendus, boucles de redirection et téléchargements excessifs. Filtrer également après résolution et redirection pour éviter un contournement.

Les contenus externes ne peuvent pas élargir les permissions, créer un outil ou changer la portée de recherche. Refuser actions inconnues et versions de protocole incompatibles avec message explicite.

Aucun secret dans prompts, logs, exports ordinaires ou manifestes portables. Si une clé de recherche est nécessaire, proposer saisie par session ou stockage sécurisé dépendant de l’OS ; préciser qu’elle ne voyage pas par défaut avec le dossier.

Accessibilité minimale dès phase 1 : navigation clavier, focus visible, noms accessibles, états annoncés sans lire chaque token, erreurs actionnables et aucun sens transmis seulement par couleur. La voix est facultative, pas un remplacement de l’interface accessible.

## C19 : distribution, qualification et décisions ouvertes

La portabilité est une exigence de phase 1, à requalifier à chaque ajout de runtime. Lire le [contrat de portabilité](portabilite.md) avant toute décision de chemins, packaging, sauvegarde ou dépendance native.

Pour déclarer un profil supporté, consigner OS/version, architecture CPU, instructions minimales, pilote, runtime, modèle/checksum, quantification, contexte, paramètres et mesures. Le fait qu’un composant amont annonce un backend ne valide pas toute la chaîne Thot.

Les résultats d’essais rapportés par l’utilisateur sont des acquis à reproduire, pas des erreurs à nier, ni des benchmarks universels. Les choix non mesurés restent ouverts dans le [registre de décisions](decisions.md), avec un test et un critère de clôture.

## Navigation

Lire ensuite les [cinq phases](README.md), le [pseudo-code](pseudocode/README.md), les [annexes](annexes/README.md) et la [matrice de tests](tests_acceptation.md). L’[audit](audit_preproduction.md) explique les corrections par rapport à la version précédente.
