# Pseudo-code contractuel — Sommaire et mode d’emploi

Ce dossier contient dix fichiers Markdown décrivant une **cible conceptuelle**, non du code exécutable. Le pseudo-code n’a pas été exécuté ni testé ; une relecture documentaire ne certifie ni l’application existante, ni les modèles, ni un paquet portable. Les résultats attendus restent à vérifier dans la [matrice d’acceptation](../tests_acceptation.md).

Les [contrats transversaux C01–C19](../contrats_transversaux.md) sont l’autorité de conception. En cas de contradiction : exigence utilisateur explicite, contrats, document de phase, puis pseudo-code. Les [annexes techniques](../annexes/README.md) étayent les choix, sans décider seules du produit ni transformer un essai rapporté en benchmark universel.

## Conventions et responsabilités

- **QUI APPELLE** nomme le demandeur du flux ; **POURQUOI** explicite sa justification ; **NE PAS CONFONDRE** sépare les responsabilités proches.
- Les blocs sont exclusivement marqués `text` et rédigés en français avec SI, POUR, CONTRAT et arrêts explicites ; les identifiants de protocole conservent leur orthographe contractuelle.
- `[Phase N]` indique l’apparition de la capacité, pas sa présence vérifiée dans l’application actuelle.
- Chaque fichier expose entrées, sorties, flux principal, erreurs/arrêts et tests ; les renvois Cxx visent les contrats, les identifiants T-C01 ou T-M11 visent des tests existants, non des résultats acquis.
- Les noms d’opérations non fixés par C08 décrivent des intentions d’API à versionner ; ils ne prétendent pas être des commandes déjà disponibles.
- **Entrées du dossier** : demande utilisateur, contrats, phases actives, profil et décisions ouvertes.
- **Sorties du dossier** : enchaînements à implémenter, invariants et critères vérifiables, sans modification de l’application.

## Ordre de lecture et de travail

Lire les [contrats](../contrats_transversaux.md), la [portabilité](../portabilite.md), les [décisions](../decisions.md) et les [tests](../tests_acceptation.md) avant d’implémenter ; puis suivre le socle, le stockage, la boucle et les outils, avec le frontend en parallèle de chaque phase.

| Fichier conservé | Responsabilité cible | Activation et dépendances |
| :--- | :--- | :--- |
| [backend_socle.md](backend_socle.md) | Cycle de vie, lecteur de contrôle disponible, writer unique, admission de run et locations de runtime | [Phase 1], extensions [Phase 2] à [Phase 5] |
| [backend_db.md](backend_db.md) | Conversation, Run, Message, Artifact, Blob, ArtifactLink, preuves, journal et snapshots | [Phase 1], migrations par capacité |
| [backend_agent_loop.md](backend_agent_loop.md) | Base partagée unique, accumulation appels/résultats, brief contraint, génération et compaction | [Phase 1], mêmes invariants jusqu’à [Phase 5] |
| [backend_outils.md](backend_outils.md) | Registre, droits, catalogue, lecture textuelle, PDF avant OCR et vision bornée | [Phase 1], outils ajoutés à leur phase |
| [backend_images.md](backend_images.md) | Même service pour chat et studio autonome, références, provenance et galerie | [Phase 2] |
| [backend_audio_documents.md](backend_audio_documents.md) | TXT/Markdown/CSV/PDF structurés, export, dictée finale et lecture vocale CPU | [Phase 3] |
| [backend_recherche.md](backend_recherche.md) | Fiches versionnées, cascade, cache frais, recherche simple/approfondie et citations | [Phase 4], périmètre espace en [Phase 5] |
| [backend_espaces.md](backend_espaces.md) | Partages par liens, promotion explicite, révisions, pertinence et pagination | [Phase 5] |
| [frontend.md](frontend.md) | Pont Rust supervisé, états/déduplication, tous les parcours et accessibilité | [Phase 1] à [Phase 5] |
| [README.md](README.md) | Lecture, carte de responsabilités, correspondance avec le prototype et limites | Toutes les phases |

Les phases sont cumulatives sans activation prématurée : catalogue et droits en phase 1, galerie image en phase 2, documents/voix en phase 3, sémantique/recherche en phase 4, espaces/mémoire en phase 5. Une migration explicite vaut mieux qu’une table future déjà peuplée sans capacité active. [C02 et C10](../contrats_transversaux.md)

## Flux principal de bout en bout

```text
[Phase 1]
CONTRAT C01 à C12, C18, C19
QUI APPELLE : utilisateur vers UI, puis Rust, coordinateur et services backend.
POURQUOI : relier les fichiers de ce dossier en un seul parcours cohérent.
NE PAS CONFONDRE : documentation cible et comportement du prototype.

AU LANCEMENT
    Rust supervise le sidecar ; le backend résout sa racine et ses droits.
    Verrouiller la racine, migrer le schéma requis et récupérer les runs interrompus.
    Vérifier protocole et capacités ; annoncer backend.ready sans promettre un modèle absent.

À L’IMPORT PUIS À L’ENVOI DU CHAT
    Copier les pièces jointes dans le stockage géré et enregistrer leurs liens.
    Dédupliquer request_id et empreinte ; persister le message utilisateur une fois.
    Inclure version, méthode et acteur/périmètre dans l’empreinte ;
    revalider les droits avant restitution d’un résultat mémorisé.
    Garantir atomiquement un seul chat non terminal par conversation à l’acceptation.
    Refuser le second sans insertion de message, pas seulement par un bouton désactivé.
    Retourner l’acceptation et run_id ; garder contrôle et navigation disponibles.
    Admettre le run lourd UNE FOIS ; ses sous-opérations réutilisent ce jeton.
    Sérialiser le contexte par mutex applicatif, sans transaction SQL longue.
    Compacter si nécessaire AVANT de figer SharedContext et réserver les ajouts.
    Accumuler appels validés, résultats, preuves et artefacts à travers les décisions.
    Chaque sous-opération acquiert puis rend sa location runtime, sans imbrication.
    Après vision/image, réacquérir le texte ; aucun ancien handle réutilisé.
    Fournir au générateur la MÊME SharedContext et un brief de champs autorisés.
    Garder le raisonnement privé hors du prompt générateur, des messages et du journal.
    Persister réponse publique, journal structuré et état avant le terminal.
    L’UI trie et déduplique par run_id/seq puis affiche le statut réel.
    La reprise utilise seq durable, vue cohérente et watermark, sans perdre
    les événements arrivés pendant la lecture de cette vue.

[Phase 2]
    Ajouter generate_image et un studio direct appelant le même service.
    Le studio utilise un run production sans conversation/message artificiels.
    Publier dans la bibliothèque personnelle ; rattacher au chat seulement explicitement.
[Phase 3]
    Produire des documents depuis schémas inertes et les mêmes artefacts.
    La voix est une commande utilisateur ; fichiers audio référencés, pas octets JSON.
[Phase 4]
    Rechercher contexte, pièces jointes et cache frais avant réseau autorisé.
    Boucler sur les lacunes dans un budget global, conserver preuves et contradictions.
[Phase 5]
    Insérer fichiers utilisateur puis mémoire de l’espace avant réseau.
    Filtrer droits et pertinence avant utilité ; paginer et promouvoir explicitement.
```

## Erreurs et arrêts transversaux

```text
[Phase 1] [Phase 2] [Phase 3] [Phase 4] [Phase 5]
CONTRAT C04 à C11, C15, C17, C18, C19

SI commande invalide ou permission absente
    Refuser avant l’effet ; ne pas compter sur la seule UI.
SI limite de contexte ou de budget
    Signaler le manque ; ne pas changer la base d’un seul rôle ni tronquer en silence.
    Sans réserve finale de tokens et durée, publier une limite déterministe,
    pas un dernier appel LLM hors budget.
SI annulation
    Garder le contrôle actif ; coopérer puis arrêter le worker isolé si nécessaire.
    Attendre fin native avant ressource libre ; pas de timeout de thread présenté comme arrêt.
    Ne pas lancer automatiquement le générateur après annulation.
SI publication interrompue
    Réconcilier les effets idempotents et conserver les sorties déjà committées.
    Collecter les orphelins sous barrière de maintenance exclusive,
    jamais pendant publication, lecture/export ou création de liens.
SI crash
    Marquer interrupted et signaler RUN_INTERRUPTED via run.failed à la reprise.
    Ne pas rejouer automatiquement un effet incertain.
SI déplacement d’espace avec extraits privés dans anciens messages/snapshots
    Refuser si indépendance non démontrée ; proposer transfert sélectif autorisé
    vers une nouvelle conversation, avec original intact.
SI portable_strict non inscriptible ou nettoyage non confirmé
    Refuser le lancement ou le déplacement ; aucun repli caché vers un autre dossier.
```

## Correspondance avec l’application existante, non modifiée

Cette carte indique où préparer le futur travail, **pas où les contrats seraient déjà implémentés**. Les modules applicatifs cités restent inchangés par cette réécriture.

| Pseudo-code | Points d’entrée réellement présents à relire | Travail cible, non certification |
| :--- | :--- | :--- |
| Socle | [main Python](../../backend/main.py), [IPC](../../backend/inference/ipc.py), [gestionnaire](../../backend/inference/manager.py), [matériel](../../backend/inference/hardware.py), [chemins](../../backend/config/paths.py) | Séparer coordination et calcul natif, versionner le contrôle et gérer les locations |
| Boucle | [boucle](../../backend/inference/loop.py), [compaction](../../backend/inference/compaction.py), [prompts](../../backend/prompts/), [dépôt conversations](../../backend/db/repositories/conversations.py) | Partager une base unique, journaliser sans raisonnement, compacter sans destruction |
| Outils | [registre](../../backend/tools/registry.py), [base](../../backend/tools/base.py), [lecture](../../backend/tools/read_text.py), [vision](../../backend/tools/vision_general.py), [OCR](../../backend/tools/vision_ocr.py) | Valider IDs, portée, budgets, extraction native et retours corrélés |
| Stockage | [modèles](../../backend/db/models.py), [base](../../backend/db/database.py), [médias](../../backend/db/media_cache.py), [fichiers](../../backend/utils/files.py) | Migrer les anciennes notions vers Artifact/Blob/ArtifactLink, pas les doubler |
| Frontend et Rust | [pont Rust](../../src-tauri/src/lib.rs), [pont UI](../../frontend/src/lib/tauri.ts), [types IPC](../../frontend/src/types/ipc.ts), [chat](../../frontend/src/pages/chat/ChatConversation.tsx), [historique](../../frontend/src/pages/history/HistoryPage.tsx), [pièces jointes](../../frontend/src/features/attachments/) | Backend unique d’autorisation/stockage, cycle de vie, tri des états et accessibilité |
| Images, audio/documents, recherche, espaces | Réutilisation des points précédents ; pas de correspondance inventée avec un module futur | Extensions conceptuelles aux phases prévues, services et vues à construire |

Le prototype conserve notamment des noms d’événements historiques et des modèles `Source` ; leur présence ne garantit pas la compatibilité C08/C10. L’adaptateur et la migration devront être explicites, avec contrôle des données déjà enregistrées. [Pont actuel](../../frontend/src/lib/tauri.ts), [modèles actuels](../../backend/db/models.py) et [contrats C08–C11](../contrats_transversaux.md)

## Décisions et ambiguïtés à garder visibles

| Sujet | Interprétation retenue dans ce dossier | Limite ou clôture attendue |
| :--- | :--- | :--- |
| Admission et location | Admission lourde une fois par run ; locations exclusives par inférence, sans réadmission dans les outils | Mécanisme worker à prototyper, arrêt natif prouvé selon [C07](../contrats_transversaux.md) et [décision D02](../decisions.md) |
| Studio sans conversation | Run de production, champ conversation_id présent mais nullable, portée bibliothèque personnelle explicite | Ambiguïté résolue par [C08/C10/C13](../contrats_transversaux.md) et [D09](../decisions.md), aucun faux chat |
| Compaction et ajouts du tour | Réserve avant gel ; gros résultats stockés hors prompt ; même révision pour les deux rôles | Profil réel et budget restent à mesurer selon [C06/C19](../contrats_transversaux.md) |
| Terminal et livraison | Un terminal logique ; interrupted récupéré signalé par run.failed/RUN_INTERRUPTED | L’état durable prévaut sur une livraison de transport incertaine selon [C08/C09](../contrats_transversaux.md) |
| Reprise et maintenance | Vue durable avec watermark ; séquences persistées ; collecte sous barrière exclusive après publications et accès actifs | Ne pas remplacer ces garanties par un tri local ou un simple délai de purge selon [C08/C11](../contrats_transversaux.md) |
| Messages de service et audio | backend.ready n’est pas un événement de run ; audio commandé par utilisateur, réponse corrélée possible | Schémas détaillés hors run et nomenclature des commandes audio encore à versionner selon [C08/C14](../contrats_transversaux.md) |
| Budgets, cache et classement | Limites configurables, fraîcheur par type de fait, pertinence avant utilité, pagination stable | Pas de durée universelle ou de score « vrai » ; mesurer selon [C04/C15–C17](../contrats_transversaux.md) et [questions O11/O12](../decisions.md) |
| Déplacement entre espaces | Vérifier texte des messages/snapshots autant que liens ; refus si indépendance non démontrée | Transfert fin avec revue de divulgation reste une extension selon [C17](../contrats_transversaux.md) |
| Paquet, modèles et matériel | Profils et chemins portables dès phase 1 ; tests à rejouer à chaque runtime ajouté | Aucun OS, modèle, mémoire ou débit certifié par le pseudo-code ; voir [portabilité](../portabilite.md) et [questions ouvertes](../decisions.md) |

## Tests et contrôle de lecture

Chaque fichier rattache ses scénarios à la [matrice d’acceptation](../tests_acceptation.md). Commencer par contrats avec doubles, puis intégration sans modèle, inférence réelle et paquet sur machine propre ; les familles T-C, T-K, T-R et T-F précèdent T-I, T-D/T-A, T-W puis T-M, tandis que T-U et T-P restent transversales.

```text
[Phase 1] [Phase 2] [Phase 3] [Phase 4] [Phase 5]
CONTRAT C01, C02, C19
QUI APPELLE : relecteur documentaire puis responsable d’implémentation.
POURQUOI : ne pas confondre un lien correct et un test fonctionnel réussi.

POUR CHAQUE FICHIER
    Vérifier présence du flux principal, erreurs, arrêts, interfaces et phases.
    Relire les Cxx applicables dans leur version courante.
    Vérifier que chaque identifiant T cité existe dans tests_acceptation.md.
    Résoudre les liens depuis ce sous-dossier, pas depuis la racine du dépôt.
    SI un contrat ou une décision change, corriger les pseudo-codes dépendants.
    Ne déclarer réussi qu’un essai réellement exécuté avec sa preuve.
```

Parcours transversaux minimaux : **T-C04/T-C05** pour contexte et accumulation, **T-K02/T-K04** pour snapshots, **T-R02/T-R04** pour arrêt et tri, **T-R10 à T-R14** pour admission, reprise, finalisation et maintenance, **T-F04** pour PDF, **T-I01/T-I08** pour studio, **T-A02** pour brouillon, **T-W02/T-W09** pour recherche, **T-M05/T-M06/T-M11** pour pertinence, pagination et transfert sûr, puis **T-P02/T-P06** et **T-U01/T-U02** pour portabilité et accessibilité. Tous restent à exécuter selon la [matrice de tests](../tests_acceptation.md).

## Bilan de relecture documentaire

Le contrôle final de ce dossier a porté sur les dix fichiers conservés : 50 blocs `text`, 160 liens locaux résolus et 96 identifiants de tests existants cités, intervalles développés compris. Aucun lien absent, identifiant de test inconnu, bloc d’un autre langage, titre numéroté, emoji ou italique n’a été détecté ; le contrôle des erreurs d’espacement du diff est sans anomalie.

Ces contrôles sont documentaires uniquement. Aucun test applicatif, matériel ou de packaging n’a été lancé ; les répertoires applicatifs backend, frontend et Rust n’ont pas été modifiés par cette intervention. Les décisions et les points restant à préciser sont consignés ci-dessus, sans promesse d’implémentation.

## Navigation

[Documentation générale](../README.md) · [Contrats](../contrats_transversaux.md) · [Portabilité](../portabilite.md) · [Décisions](../decisions.md) · [Tests](../tests_acceptation.md) · [Annexes](../annexes/README.md).
