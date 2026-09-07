# Phase 1 — Socle portable, chat agentique et vision

## Statut et résultat attendu

Ce document spécifie une cible de préproduction : il ne certifie ni l’achèvement de l’ancien prototype ni ses performances.
L’ordre de vérité est celui de [C01](contrats_transversaux.md#c01--vocabulaire-et-ordre-de-vérité) : exigence utilisateur, contrats, phase, puis pseudo-code.
Le résultat attendu est une conversation locale persistante, capable de lire des pièces jointes et de reprendre après interruption sur un profil matériel qualifié.
La portabilité, les droits et les erreurs explicites commencent ici ; ils ne sont pas reportés à l’arrivée des espaces ([C02](contrats_transversaux.md#c02--activation-par-phase)).

## Objectifs et hors périmètre

- Permettre création, reprise, navigation et suppression contrôlée des conversations, avec saisie et réponse progressive accessibles au clavier.
- Importer images, PDF et textes dans un stockage géré ; montrer disponibilité, provenance et ambiguïtés avant de sélectionner une ressource.
- Séparer décision d’outils, exécution autorisée et rédaction, sans exposer ni conserver par défaut le raisonnement privé.
- Conserver l’historique original malgré la compaction et viser le contexte de 128k avec un protocole reproductible.
- Distribuer une première application transportable avec ses données, sous les limites déclarées du [contrat de portabilité](portabilite.md).

Hors périmètre : génération d’images, fichiers produits, voix, recherche web, descriptions sémantiques riches et mémoire d’espace.
Réserver les identifiants et migrations utiles sans créer dès maintenant toutes les tables ni activer les capacités des phases suivantes ([C02](contrats_transversaux.md#c02--activation-par-phase)).
Une cible « profil 8 Go » n’équivaut pas à une promesse de fonctionnement universel sur tout GPU, CPU ou système.

## Prérequis et décisions d’entrée

1. Fixer une racine de données, les règles de sauvegarde et les dépendances distribuables dans [portabilite.md](portabilite.md).
2. Identifier les checkpoints texte, vision et OCR, leurs empreintes, licences, templates, tokenizers et composants associés.
3. Préparer un corpus versionné : texte UTF-8, PDF natif, scan, PDF mixte, images ambiguës, fichier malformé et historique long.
4. Définir des profils distincts NVIDIA, AMD, Apple et CPU avec OS, architecture, instructions CPU minimales, pilotes et runtime.
5. Prototyper un coordinateur Python réactif et des workers d’inférence isolables ; consigner le choix et ses limites dans [decisions.md](decisions.md).

Qualifier d’abord le paquet Windows portable sur machine propre, sans Python, Node ni chaîne de compilation installés ; Linux et macOS restent des cibles distinctes à éprouver ([portabilité](portabilite.md)).
Tester le déplacement application fermée, workers arrêtés et base cohérente, y compris vers une autre lettre de lecteur ; ne pas confondre déplacement des données et lancement du paquet complet.
LFM2.5-8B-A1B, LFM2.5-VL-3B et GLM-OCR sont les références à vérifier avec leurs composants exacts, non des intégrations présumées terminées ([annexe 01](annexes/01_lfm_et_architecture.md), [annexe 02](annexes/02_quantification_gguf.md)).
Le support annoncé par un moteur ne qualifie pas toute la chaîne Thot ; seul un profil effectivement testé peut être déclaré supporté ([C19](contrats_transversaux.md#c19--distribution-qualification-et-décisions-ouvertes)).

## Responsabilités, entrées et sorties

| Responsable | Entrées autorisées | Sorties et frontière |
| :--- | :--- | :--- |
| Interface React | Saisie, fichiers choisis, commandes utilisateur | Commandes Tauri ; affichage par conversation/run, sans décision de droits |
| Pont Tauri/Rust | Commandes validées côté interface | Requêtes JSON Lines versionnées ; gestion du sidecar |
| Coordinateur Python | Requête, état durable, profil et budgets | Run, file bornée, annulation, réponses et événements corrélés |
| Orchestrateur | `SharedContext`, système propre, outils autorisés, résultats du tour | Appels structurés ou brief contraint, jamais accès direct au disque |
| Exécuteur déterministe | Outil connu, arguments et identifiants | Résultat corrélé, preuve ou erreur après vérification de portée |
| Générateur sans outils | Même `SharedContext`, système propre, brief et observations validés | Réponse publique avec limites et références utiles |
| Compacteur | Historique public borné, snapshot antérieur | Nouveau snapshot validable, sans remplacement des originaux |
| Stockage et ordonnanceur | Effets validés, demande de runtime | Écritures courtes, location exclusive et handles versionnés |

Ces frontières appliquent [C03–C08](contrats_transversaux.md#c03--identité-du-contexte-et-séparation-des-rôles) ; les détails de séquencement sont dans le [pseudo-code du socle](pseudocode/backend_socle.md) et de la [boucle](pseudocode/backend_agent_loop.md).

## Contexte commun, boucle et journal

Pour chaque tour, construire une seule base `SharedContext(conversation_id, revision)` : résumé actif, échanges récents ordonnés, message courant exactement une fois et catalogue compact.
L’orchestrateur et le générateur reçoivent cette même révision ; leurs prompts système et leurs ajouts restent distincts ([C03](contrats_transversaux.md#c03--identité-du-contexte-et-séparation-des-rôles)).
Le brief accepte seulement `user_goal`, `response_constraints`, `evidence_ids`, `artifact_ids`, `unresolved_points`, `execution_status`.
Valider champs, taille, provenance et droits ; en cas de brief invalide, reconstruire un brief minimal depuis la demande et les résultats autorisés.
Ne jamais transmettre au générateur le transcript brut de l’orchestrateur, les schémas d’outils ou des champs de raisonnement ([C03](contrats_transversaux.md#c03--identité-du-contexte-et-séparation-des-rôles)).

Chaque appel valide reçoit exactement un résultat via `tool_call_id`, y compris une erreur, avant la décision suivante.
Accumuler preuves et artefacts sur tout le tour ; arrêter sur brief valide, plafond, doublon sans progrès, erreur fatale ou annulation.
Après vision/OCR, réacquérir le runtime texte et reconstruire le contexte : le handle déchargé est définitivement périmé ([C04](contrats_transversaux.md#c04--boucle-outils-retour-et-arrêt)).
Persister séparément le dialogue public et le journal technique borné des appels, arguments validés, observations, erreurs et durées.
Ce journal n’est ni une chaîne de pensée ni un historique à réinjecter intégralement ; le raisonnement privé brut n’est pas collecté par défaut ([C05](contrats_transversaux.md#c05--historique-durable-et-contexte-dinférence)).

## Parcours concrets : succès et échec

### Conversation puis lecture d’un PDF

1. L’utilisateur dépose un PDF et pose une question ; le backend valide et copie le fichier, puis crée son artefact et son rattachement.
2. Le catalogue expose l’ID, le nom, le type détecté et la disponibilité ; l’orchestrateur sélectionne une plage pertinente.
3. Lire d’abord le texte natif exploitable ; ne rendre/OCRiser que les pages nécessaires, sauf besoin explicite de tableau ou de mise en page.
4. Enregistrer extrait, page et transformations ; le générateur répond depuis les observations, en signalant une lecture incertaine.
5. Après fermeture et reprise, retrouver message, artefact et localisation sans dépendre du fichier original externe ([C10–C12](contrats_transversaux.md#c10--stockage-identifiants-et-droits)).

Si deux images rendent « celle-ci » ambigu, demander laquelle ; si le PDF est illisible, annoncer la limite au lieu d’inventer son texte.
Si le projecteur ou le handler vision manque, refuser ce profil sans retirer le composant ni substituer une architecture incompatible ([C07](contrats_transversaux.md#c07--ressources-runtimes-et-concurrence), [C12](contrats_transversaux.md#c12--catalogue-lecture-et-extraction)).

### Annulation pendant OCR puis reprise

1. L’interface continue à recevoir les états pendant l’OCR ; l’utilisateur annule et peut consulter une autre conversation.
2. Le coordinateur demande l’arrêt coopératif puis termine le worker si nécessaire, dans le délai configuré.
3. Persister l’état avant l’événement terminal ; une réponse déjà commencée reste marquée partielle ou annulée.
4. Après crash, marquer le run interrompu, réconcilier les sorties et proposer une relance sans dupliquer le message utilisateur ([C08–C09](contrats_transversaux.md#c08--ipc-et-état-de-linterface)).

### Historique long et compaction

1. Compter le prompt effectif de chaque rôle, y compris système, outils, observations, réserve de sortie et marge.
2. Construire un snapshot avec borne `covered_through_message_id` ; l’activer uniquement si sa validation et la révision concordent.
3. Reprendre avec résumé et suffixe non chevauchants ; garder les messages sources consultables pour une citation exacte.
4. Si la synthèse échoue ou dépasse la fenêtre, conserver le snapshot précédent et segmenter ou proposer explicitement un profil réduit ([C06](contrats_transversaux.md#c06--compaction-non-destructive-et-budget-réel)).

## Données et protocole durables

Introduire `Conversation`, `Message`, `Run`, `Blob`, `Artifact`, `ArtifactLink`, `ToolCall`, `Evidence` et `ContextSnapshot` par schémas versionnés ([C10](contrats_transversaux.md#c10--stockage-identifiants-et-droits)).
Les chemins durables sont relatifs à la racine gérée ; un hash identifie des octets, jamais un droit d’accès.
Publier un fichier via préparation, contrôle, empreinte et commit court ; n’émettre `artifact.created` qu’après validation durable ([C11](contrats_transversaux.md#c11--publication-de-fichiers-et-suppression)).
L’IPC transporte IDs et aperçus bornés, sans PDF complet ni médias en base64 ; logs sur stderr et sérialiseur stdout unique.
Les requêtes portent `protocol_version`, `request_id`, `method`, `params` ; les événements portent `run_id`, `conversation_id`, `seq` et `payload`.
`chat.send` accuse réception ; `answer.delta` diffuse la réponse ; un seul événement terminal clôt le run après persistance ([C08](contrats_transversaux.md#c08--ipc-et-état-de-linterface)).
Une reconnexion recharge l’état durable ; rejeter doublons et événements tardifs d’un autre run plutôt que les afficher dans le chat courant.
La suppression retire les liens concernés et ne détruit pas les blobs encore référencés ; les temporaires orphelins relèvent d’une maintenance séparée ([C09–C11](contrats_transversaux.md#c09--annulation-reprise-et-idempotence)).

## Matériel, sécurité et limites explicites

Une seule famille de pipeline reçoit la location GPU sur le profil 8 Go ; plusieurs composants nécessaires peuvent appartenir à cette famille.
La cible de référence reste une consommation inférieure à 8 Go de VRAM dédiée pour le profil concerné, composants, buffers et contexte compris ; elle doit être mesurée, pas déduite de la taille des poids ([portabilité](portabilite.md)).
Mesurer séparément VRAM dédiée, mémoire unifiée Apple, RAM, disque et fichiers mappés ; ne pas déduire un profil AMD ou CPU d’une détection NVIDIA.
Garder le coordinateur non bloquant, la file bornée et les workers isolables ; ne charger la famille suivante qu’après confirmation de libération ([C07](contrats_transversaux.md#c07--ressources-runtimes-et-concurrence)).
La cible 128k et l’essai utilisateur rapporté sont conservés et doivent être reproduits avec modèle, quantification, tokenizer, backend et paramètres exacts.
Ni l’architecture ni le nombre de paramètres actifs ne permettent ici d’affirmer une absence de cache KV ou un coût mémoire nul du contexte ([annexe 01](annexes/01_lfm_et_architecture.md), [C19](contrats_transversaux.md#c19--distribution-qualification-et-décisions-ouvertes)).
Le seuil envisagé autour de 110 000 jetons est préventif et configurable ; il ne remplace pas le comptage du rôle suivant ([C06](contrats_transversaux.md#c06--compaction-non-destructive-et-budget-réel)).
Valider MIME réel, taille, noms, chemins résolus, liens symboliques et autorisations à chaque lecture/export ; les pièces jointes restent des données non fiables.
Prévoir focus visible, libellés accessibles, annonces d’état non bavardes et erreurs actionnables ; ne stocker aucun secret dans les prompts ou manifestes ([C18](contrats_transversaux.md#c18--sécurité-confidentialité-et-accessibilité)).

## Exigences → conséquences → tests

Les implications de conception sont explicites ; les critères ci-dessous sont à exécuter, sans résultat mesuré dans ce document.

| Exigence | Conséquence technique ou produit | Test d’acceptation proposé |
| :--- | :--- | :--- |
| Même contexte, rôles distincts — C03 | Base unique versionnée, ajouts contrôlés | Sur 20 tours avec/sans outils : même révision et message courant présent une fois |
| Traces utiles, pas de pensée — C05 | Journal technique séparé du dialogue | Sur des cas de test à canaux mixtes : zéro raisonnement brut dans message, brief ou export ordinaire |
| PDF natif avant OCR — C12 | Extraction puis rendu ciblé | PDF natif lisible : zéro appel OCR ; scan ciblé : pages traitées conformes à la sélection |
| Droits dès le socle — C10/C18 | Résolution des IDs côté backend | Sur tous les cas d’accès inter-conversation interdits : zéro octet retourné |
| Grand contexte conservé — C06/C19 | Profil 128k explicite et mesure par rôle | Reproduire l’essai utilisateur ; relever pics mémoire, temps et qualité sans dépassement du profil déclaré |
| Compaction non destructive — C06 | Snapshot transactionnel, originaux intacts | Sur 10 compactions, dont échec et concurrence : zéro message perdu ou doublé |
| Annulation effective — C07/C09 | Arrêt natif vérifiable, pas seulement coroutine fermée | Sur 10 arrêts : worker arrêté dans le délai configuré et aucun chargement concurrent |
| IPC robuste — C08 | Corrélation, séquence, limites et resynchronisation | Injecter doublon, delta tardif et file pleine : zéro contamination de conversation |
| Données portables — C19 | Chemins relatifs, dépendances qualifiées | Déplacer la racine vers un chemin avec espaces/accents ; rouvrir tous les artefacts du corpus |

## Jalons ordonnés et critères de sortie

Chaque jalon exige un compte rendu dans les [tests d’acceptation](tests_acceptation.md) ; aucun n’est déclaré acquis ici.

| Ordre | Livraison de conception puis validation | Critère de passage mesurable |
| :--- | :--- | :--- |
| P1-J1 | Profil, racine portable, droits et schéma initial | Manifeste complet ; 100 % des cas de refus d’accès et de chemin dangereux passent |
| P1-J2 | IPC réactif, stockage des runs et reprise | 20 envois/reprises sans duplication ; un terminal par run terminé ; annulation reçue pendant outil lent |
| P1-J3 | Boucle, contexte partagé et journal séparé | Corpus de 20 tours conforme à C03–C05 ; budgets observables et aucune fuite de pensée |
| P1-J4 | Imports, lecture native, vision et OCR | Corpus de référence traité ou refusé explicitement ; provenance présente pour chaque observation |
| P1-J5 | Compaction et essai long contexte | Tests P1 de non-perte réussis ; résultat 128k documenté, ou blocage explicite sans prétendre la cible atteinte |
| P1-J6 | Packaging et qualification du socle | Trois cycles démarrage/déplacement/reprise par profil revendiqué ; pics mémoire et délais consignés |

La phase 2 ne suppose acquis que les contrats démontrés ; tout écart conserve son statut ouvert dans le [registre de décisions](decisions.md).

## Références et suite

- [Index central](README.md) ; [README du dépôt](../README.md) pour l’état du dépôt, sans valeur de certification.
- [Contrats C01–C19](contrats_transversaux.md), [portabilité](portabilite.md), [décisions](decisions.md), [tests d’acceptation](tests_acceptation.md).
- [Socle](pseudocode/backend_socle.md), [boucle](pseudocode/backend_agent_loop.md), [outils](pseudocode/backend_outils.md), [stockage](pseudocode/backend_db.md), [interface](pseudocode/frontend.md) : pseudo-code, pas programme à exécuter.
- [Annexe 01 — LFM](annexes/01_lfm_et_architecture.md), [annexe 02 — quantification](annexes/02_quantification_gguf.md), [annexe 06 — pratiques backend](annexes/06_bonnes_pratiques_python.md) : références techniques à lire avec leurs réserves.
- Suite : [phase 2 — génération d’images](phase_2_generation_image.md).
