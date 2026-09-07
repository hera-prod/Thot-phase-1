# Phase 5 — Espaces facultatifs et mémoire contrôlée

## Statut et résultat attendu

Cette phase définit le partage de ressources entre conversations et une mémoire révisable ; elle ne certifie pas un système de mémoire déjà livré.
L’utilisateur doit pouvoir travailler dans un espace ou rester hors espace, contrôler les documents partagés et décider quelles synthèses deviennent de la mémoire.
Le cadre normatif est [C17](contrats_transversaux.md#c17--espaces-et-mémoire), complété par C10–C11 pour les droits et fichiers, C15–C16 pour la recherche et C19 pour la portabilité.

## Objectifs et hors périmètre

- Créer, renommer, consulter et supprimer des espaces sans imposer le classement de tout l’historique.
- Rattacher volontairement des conversations et ressources à un espace, avec droits réévalués à chaque changement.
- Séparer une zone documentaire utilisateur et une zone mémoire de synthèses versionnées.
- Filtrer les ressources par droits et pertinence avant de les classer par utilité ; conserver recherche ciblée et pagination.
- Permettre validation, correction, invalidation, fusion, suppression et export complet des informations conservées.

Hors périmètre : synchronisation distante, collaboration multi-utilisateurs, accès global entre espaces, écriture arbitraire de l’IA dans les documents, mémoire autonome non supervisée et transfert finement audité d’un historique dépendant d’un autre espace.
La promotion automatique n’appartient pas au jalon initial ; une évolution ultérieure doit être optionnelle, auditable et explicitement activée ([C17](contrats_transversaux.md#c17--espaces-et-mémoire)).
Une mémoire fréquemment utilisée n’est ni une vérité certifiée ni un remplacement des sources primaires.

## Prérequis et décisions d’entrée

1. Valider les contrats de portée conversation, de partage par rattachement, de suppression et de publication hérités des premières phases.
2. Disposer des fiches, preuves, snapshots web et modes de recherche de phase 4 sans modifier leurs protections.
3. Préparer la migration `Workspace` et `MemoryEntry`, avec `workspace_id` facultatif sur la conversation et versions explicites.
4. Définir les transitions des fiches, l’interface de validation humaine et le traitement des références devenues indisponibles.
5. Fixer les règles de classement, pagination, rétention, fusion et comptage d’usage dans [decisions.md](decisions.md).
6. Construire un corpus à deux espaces, une conversation hors espace, des blobs partagés, des fiches contradictoires et des historiques ayant intégré des sources privées dans leurs messages ou résumés.

La migration n’impose ni table concurrente `sessions`, ni création rétroactive de tous les espaces en phase 1 ([C01–C02](contrats_transversaux.md#c01--vocabulaire-et-ordre-de-vérité)).
Les répertoires logiques doivent rester compatibles avec une racine déplaçable, pas avec des chemins absolus enregistrés comme identifiants ([C10](contrats_transversaux.md#c10--stockage-identifiants-et-droits), [portabilité](portabilite.md)).

## Responsabilités, entrées et sorties

| Responsable | Entrées | Sorties et frontière |
| :--- | :--- | :--- |
| Interface d’espaces | Actions utilisateur explicites | Création, rattachement, validation, correction, export et suppression |
| Service d’autorisation | Conversation, espace optionnel et révision de portée | Ensemble de ressources accessibles, revalidé à chaque lecture/écriture |
| Zone utilisateur `workspace_user` | Imports ou modifications par commandes utilisateur | Documents partagés, lisibles mais non modifiables directement par l’IA |
| Zone mémoire `workspace_memory` | Propositions validées par le service mémoire | Fiches structurées et versionnées, jamais chemins d’écriture libres |
| Orchestrateur | Requête, catalogue autorisé et preuves | Lecture ciblée et proposition de mémoire avec raisons structurées |
| Service mémoire | Proposition, sources, contexte et décision humaine | Candidat, version acceptée ou refusée, invalidation et fusion traçables |
| Classement | Candidats autorisés et pertinents | Pages de résultats avec utilité secondaire explicable |
| Export/suppression | Périmètre et confirmation utilisateur | Manifeste cohérent, retrait de liens, purge contrôlée et rapport |

L’utilisateur garde le contrôle des deux zones ; l’IA ne peut pas contourner `workspace_user` en écrivant directement dans ses fichiers.
Une révision proposée par l’IA est un **nouvel artefact**, puis une éventuelle modification décidée par une action utilisateur distincte ([C11](contrats_transversaux.md#c11--publication-de-fichiers-et-suppression), [C17](contrats_transversaux.md#c17--espaces-et-mémoire)).

## Données, rattachements et cloisonnement

Une conversation a zéro ou un espace ; hors espace, ses ressources restent accessibles uniquement selon ses rattachements conversation.
Dans un espace, elle accède à ses propres pièces jointes et aux ressources explicitement partagées de cet espace, pas aux pièces privées des autres conversations.
Un déplacement autorisé ne partage pas automatiquement les pièces jointes avec les autres conversations ; tout élargissement de portée reste explicite ([C10](contrats_transversaux.md#c10--stockage-identifiants-et-droits), [C17](contrats_transversaux.md#c17--espaces-et-mémoire)).
La politique initiale interdit le transfert automatique d’une conversation ayant pu intégrer des sources ou mémoires privées de l’espace d’origine, y compris dans ses anciens messages, citations et résumés.
Si son indépendance n’est pas démontrable, refuser le déplacement : retirer les seuls `evidence_ids` ou reconstruire le snapshot ne suffit pas à rendre l’historique indépendant ([C17](contrats_transversaux.md#c17--espaces-et-mémoire)).
Réutiliser `ArtifactLink` pour les rattachements ; la déduplication concerne les octets `Blob` à l’intérieur d’une même racine gérée.
Un hash identique ne confère jamais de droit et ne doit pas révéler l’existence d’un document d’un autre espace.
Ne pas recopier un fichier pour chaque conversation ; conserver les métadonnées logiques et droits indépendamment du blob partagé ([C10](contrats_transversaux.md#c10--stockage-identifiants-et-droits)).

Chaque `MemoryEntry` porte au minimum ID, espace, version, contenu synthétique, sources accessibles, contexte d’origine, date et statut.
Ajouter raisons structurées de promotion, décision utilisateur, références de preuves, liens aux versions précédentes et signaux d’usage.
Les états sont `candidate`, `accepted`, `stale`, `rejected` ; une correction, fusion ou invalidation laisse une trace versionnée sans réécrire secrètement l’historique.
Une source supprimée, une portée révoquée ou une contradiction nouvelle doit rendre la fiche révisable et empêcher une réutilisation présentée comme certaine ([C17](contrats_transversaux.md#c17--espaces-et-mémoire)).
Les versions conservées restent soumises à la suppression demandée ; « versionné » ne signifie pas rétention indéfinie contre la volonté de l’utilisateur.

## Promotion et révision de mémoire

1. L’orchestrateur propose une synthèse seulement depuis des preuves accessibles, avec contexte, date, portée et raisons de réutilisation.
2. Le service vérifie schéma, références, limites et doublons potentiels ; la proposition devient `candidate`, non une connaissance acceptée.
3. L’interface présente contenu, sources, périmètre et effets ; l’utilisateur accepte, corrige ou refuse.
4. L’acceptation crée une version `accepted` ; un refus conserve seulement ce que prévoit la politique de rétention visible.
5. Une correction ultérieure crée une version traçable ; une source périmée conduit à `stale` et à une demande de révision.
6. Une fusion doit préserver provenance et contradictions utiles, pas faire gagner silencieusement la fiche la plus populaire.

La validation humaine initiale autorise la promotion, mais n’est pas une preuve supplémentaire de vérité factuelle ([C17](contrats_transversaux.md#c17--espaces-et-mémoire)).
Un refus, une correction et une suppression doivent être accessibles sans demander au modèle de rédiger ou d’exécuter une commande.
Ne pas enregistrer en mémoire les monologues de planification, secrets ou instructions externes destinées à modifier les droits ([C03](contrats_transversaux.md#c03--identité-du-contexte-et-séparation-des-rôles), [C18](contrats_transversaux.md#c18--sécurité-confidentialité-et-accessibilité)).

## Recherche : pertinence avant utilité

La cascade conserve contexte, pièces jointes et cache web admissible de conversation, puis ajoute fichiers utilisateur et mémoire de l’espace avant réseau simple et approfondissement.
Les modes `local_only`, `web_allowed` et `deep_research`, l’accord sensible et les budgets de phase 4 restent inchangés ([C15](contrats_transversaux.md#c15--politique-de-recherche-et-suffisance)).

| Ordre de sélection | Règle | Conséquence visible |
| :--- | :--- | :--- |
| 1. Autorisation | Exclure les ressources hors périmètre avant classement | Aucun titre, aperçu ou compteur d’un autre espace |
| 2. Pertinence | Chercher les candidats répondant à la requête et aux sous-questions | Une fiche rare mais pertinente reste découvrable |
| 3. Validité et fraîcheur | Examiner statut, date, sources et contradictions | Les fiches périmées ne sont pas des preuves actuelles sans réserve |
| 4. Utilité secondaire | Fréquence d’usage réel, récence et retour explicite | Le score départage des candidats pertinents, sans valider leur vérité |
| 5. Pagination/lecture | Afficher une page bornée puis ouvrir ciblé | `k=5` peut être une taille de page, jamais une limite de découverte |

Incrémenter l’usage quand une preuve de la fiche est réellement retenue pour la réponse, pas à chaque aperçu, recherche ou consultation.
Éviter les doubles incréments lors d’une relance via une clé d’usage idempotente ; rendre les signaux consultables et corrigeables ([C09](contrats_transversaux.md#c09--annulation-reprise-et-idempotence), [C17](contrats_transversaux.md#c17--espaces-et-mémoire)).
Prévoir recherche lexicale, pagination et ouverture par ID autorisé ; ne pas limiter le prompt aux cinq fiches globalement les plus populaires.
Les réponses citent les preuves d’origine ; si elles sont indisponibles, l’indiquer au lieu de citer la synthèse comme source primaire ([C16–C17](contrats_transversaux.md#c16--cache-preuves-et-citations)).

## Parcours concrets : succès et échec

### Travailler sans espace puis partager volontairement

1. L’utilisateur ouvre une conversation historique hors espace ; elle fonctionne sans migration forcée.
2. Il crée un espace, y rattache la conversation et choisit un document à partager dans la zone utilisateur.
3. Une autre conversation de cet espace peut lire le document partagé, mais pas une autre pièce jointe restée privée.
4. Une proposition de modification devient un nouvel artefact ; le document utilisateur reste inchangé tant qu’il ne l’a pas remplacé explicitement ([C11](contrats_transversaux.md#c11--publication-de-fichiers-et-suppression), [C17](contrats_transversaux.md#c17--espaces-et-mémoire)).

### Promouvoir puis corriger une synthèse

1. Une recherche propose une fiche sourcée ; l’utilisateur la relit et accepte sa promotion.
2. Une question ultérieure pertinente retrouve la fiche même si elle est peu utilisée, puis cite la preuve réellement retenue.
3. L’utilisateur constate une erreur, corrige ou invalide la fiche ; la version obsolète n’est plus présentée comme valide.
4. Si la source a disparu ou si deux fiches se contredisent, suspendre la promotion ou demander une révision humaine, sans inventer une source ([C17](contrats_transversaux.md#c17--espaces-et-mémoire)).

### Demander un déplacement entre deux espaces

1. Examiner les dépendances à l’espace d’origine, y compris les informations déjà incorporées aux messages et résumés, pas seulement les références encore actives.
2. Si une dépendance privée est possible ou l’indépendance indémontrable, refuser le déplacement sans modifier la conversation d’origine.
3. Proposer une nouvelle conversation dans la destination ; l’utilisateur sélectionne explicitement les messages et artefacts dont la reprise est autorisée.
4. Revalider chaque élément sélectionné, préserver sa provenance et exclure les résumés, mémoires ou citations privées non autorisés ; ne rien recopier implicitement.
5. Si l’indépendance est démontrée, demander confirmation puis recalculer les droits et reconstruire le contexte autorisé ; un transfert audité plus fin reste une option future ([C03](contrats_transversaux.md#c03--identité-du-contexte-et-séparation-des-rôles), [C17](contrats_transversaux.md#c17--espaces-et-mémoire)).

## Suppression, déduplication et export complet

Supprimer une conversation retire ses liens et son historique selon le choix utilisateur ; ne pas supprimer un blob encore utilisé par un espace ou une autre ressource.
Purger la mémoire ne doit pas altérer la zone utilisateur ; supprimer une source nécessite d’indiquer ses dépendances mémoire et versions conservées.
Après confirmation, retirer ou invalider les références concernées et appliquer la politique de purge ; aucune récupération ne doit ressusciter un contenu supprimé ([C11](contrats_transversaux.md#c11--publication-de-fichiers-et-suppression)).
Le nettoyage distingue blob sans référence, cache expiré, temporaire et artefact publié ; il est borné, journalisé et indépendant des décisions du LLM.
La déduplication doit conserver un blob tant qu’au moins un rattachement autorisé ou une dépendance conservée le requiert, sans révéler ces liens aux autres espaces ([C10–C11](contrats_transversaux.md#c10--stockage-identifiants-et-droits)).

L’export complet de la racine doit permettre de reconstituer conversations, espaces, ressources conservées, rattachements, mémoire et versions, preuves et snapshots encore retenus.
Inclure un manifeste versionné, chemins relatifs, empreintes et états de disponibilité ; exclure clés et secrets, et distinguer sauvegarde de données et distribution de modèles.
Un export partiel d’espace ou de conversation inclut ses dépendances autorisées, ou une liste explicite des manques ; il ne doit pas embarquer des données privées d’un autre périmètre.
Vérifier la cohérence entre base et fichiers pendant l’export, puis tester une restauration dans une nouvelle racine ; ne pas promettre une sauvegarde complète d’après un simple dossier copié à chaud ([C17–C19](contrats_transversaux.md#c17--espaces-et-mémoire), [portabilité](portabilite.md)).

## Sécurité et limites

Le backend contrôle droits, chemins résolus, liens symboliques et versions de portée ; masquer une ressource dans l’interface ne suffit pas à la protéger.
Les recherches, aperçus, exports, promotions et lectures de preuves doivent tous repasser cette frontière d’autorisation.
Les instructions contenues dans une fiche ou une source ne peuvent devenir des permissions, même après validation de la fiche par l’utilisateur ([C18](contrats_transversaux.md#c18--sécurité-confidentialité-et-accessibilité)).
La mémoire compacte n’assure pas un rappel parfait ; conserver la possibilité d’ouvrir les sources et de signaler une référence absente.
Toute opération destructive ou de partage doit afficher son périmètre et rester disponible au clavier ; aucun score ne doit décider seul de supprimer un document.

## Exigences → conséquences → tests

Ces critères sont à exécuter ; ni le classement, ni la confidentialité, ni la restauration ne sont déclarés validés ici.

| Exigence | Conséquence technique ou produit | Test d’acceptation proposé |
| :--- | :--- | :--- |
| Espace facultatif — C17 | `workspace_id` nullable et migration non forcée | Reprendre 10 conversations hors espace sans création automatique d’espace |
| Deux zones contrôlées — C11/C17 | IA en lecture seule sur documents utilisateur | Toutes les tentatives d’écriture IA refusées ; une révision proposée crée un nouvel artefact |
| Promotion humaine initiale — C17 | Candidat avant acceptation | 10 propositions : aucune promue sans action utilisateur ; refus et correction conservés selon politique |
| Pertinence avant utilité — C17 | Filtrage puis classement secondaire | Corpus avec fiche pertinente peu populaire : elle reste trouvable au-delà de la première page |
| Usage réel seulement — C17 | Compteur sur preuve retenue | 10 consultations sans usage : zéro incrément ; relance d’une réponse : un seul incrément |
| Cloisonnement — C10/C18 | Droits sur candidats, preuves et exports | Tous les accès croisés interdits échouent sans fuite de titre, contenu ou existence |
| Déplacement conservateur — C17 | Refus si indépendance indémontrable, même sans `evidence_ids` | Cas avec citation privée dans ancien message ou résumé : déplacement refusé ; original intact ; zéro transfert implicite |
| Reprise explicite en destination — C17 | Nouvelle conversation et sélection autorisée avec provenance | Tous les éléments repris proviennent de la sélection utilisateur autorisée ; aucun ancien résumé privé ajouté |
| Suppression et déduplication — C11 | Graphe de références et purge | Supprimer un lien partagé conserve le blob ; supprimer la dernière dépendance suit la politique annoncée |
| Export complet — C19 | Manifeste et restauration testable | Export/restauration en nouvelle racine : IDs, liens, versions et empreintes identiques pour les éléments conservés |

## Jalons ordonnés et critères de sortie

| Ordre | Livraison attendue | Critère de passage à mesurer |
| :--- | :--- | :--- |
| P5-J1 | Migration facultative et droits | Corpus hors espace intact ; tous les accès croisés non autorisés refusés |
| P5-J2 | Zones et partage par rattachement | Documents partagés accessibles sans recopies ; originaux inchangés après toutes les propositions IA |
| P5-J3 | Candidats, validation et révisions | 10 cycles proposer/accepter/corriger/refuser ; zéro promotion automatique initiale |
| P5-J4 | Recherche contextualisée et pagination | Fiche rare trouvable ; aucun usage compté à la seule consultation ; citations primaires conservées |
| P5-J5 | Déplacement conservateur, suppression et dépendances | Matrice A/B/hors espace : refus de tout historique non indépendant ; reprise sélective autorisée et blobs partagés testés |
| P5-J6 | Export, restauration et qualification | Trois restaurations dans des racines déplacées ; intégrité de tous les éléments conservés vérifiée |

Inscrire les résultats et les écarts dans les [tests d’acceptation](tests_acceptation.md) et le [registre de décisions](decisions.md), notamment poids du classement, rétention et automatisations futures.

## Références et clôture du parcours

- [Index central](README.md), [phase 4](phase_4_moteur_recherche.md), [contrats C01–C19](contrats_transversaux.md), [portabilité](portabilite.md).
- [Annexe 07 — recherche et mémoire](annexes/07_recherche_agents_memoire.md), [annexe 06 — pratiques backend](annexes/06_bonnes_pratiques_python.md).
- [Pseudo-code espaces](pseudocode/backend_espaces.md), [recherche](pseudocode/backend_recherche.md), [stockage](pseudocode/backend_db.md), [interface](pseudocode/frontend.md) : descriptions subordonnées aux contrats.
- La fin des cinq phases exige des preuves d’acceptation cumulées, pas la seule présence des écrans ou du pseudo-code.
