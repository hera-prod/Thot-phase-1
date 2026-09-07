# Portabilité et distribution de Thot

L’objectif utilisateur est concret : disposer d’un dossier Thot que l’on peut déplacer, copier sur une clé USB puis lancer sur une autre machine compatible. Ce document transforme cet objectif en contrat de conception et en essais de qualification ; aucune plateforme n’est déclarée validée sur la seule base de cette documentation.

## Trois promesses à ne pas confondre

- **Multiplateforme** : proposer des distributions adaptées à Windows, Linux et macOS, avec profils CPU et accélérateurs qualifiés séparément. Cela ne signifie pas livrer le même exécutable sur tous les systèmes.
- **Application portable** : déplacer toute la distribution sans installer Python, Node ou une chaîne de compilation chez l’utilisateur, et sans dépendre d’un ancien chemin absolu.
- **Données portables** : emporter conversations, fichiers importés, créations et mémoire dans un ensemble cohérent, puis les retrouver au prochain lancement.

La cible initiale est une distribution Windows par dossier avec données autonomes, puis une qualification Linux et macOS de même intention. L’ordre de qualification est une proposition de production, pas l’abandon du multiplateforme ; les [décisions ouvertes](decisions.md) fixent ce qui doit encore être démontré.

## Contrat de lancement portable

En mode `portable_strict`, l’utilisateur ferme Thot, copie ou déplace le dossier complet, puis lance l’exécutable destiné à son système et architecture. L’application résout sa racine depuis son propre emplacement, jamais depuis le répertoire de travail du terminal ou un chemin stocké sur la machine précédente.

La distribution doit comprendre les exécutables applicatifs, les bibliothèques embarquables nécessaires, un manifeste de compatibilité et, dans l’édition autonome, les modèles retenus. Les éventuels prérequis hôtes non embarqués sont annoncés avant téléchargement et diagnostiqués au lancement.

La promesse est conditionnelle à une machine compatible : OS, architecture CPU, instructions CPU, pilotes, droits d’exécution, stockage inscriptible et ressources suffisantes. « Portable » ne veut pas dire « exécutable sur n’importe quelle machine » ni « indépendant des politiques de sécurité de l’ordinateur ».

## Organisation logique du dossier

Arborescence indicative de distribution, non scaffold applicatif à créer maintenant :

```text
Thot/
    lanceur adapté à la plateforme
    app/                 ressources applicatives, workers et dépendances
    models/              poids, projecteurs, tokenizers et configurations
    licenses/            licences applicatives, bibliothèques, modèles, voix
    distribution.json    manifeste de plateforme, versions et contrôles
    portable.json        intention portable et version du format
    data/
        database/        base et éventuels fichiers de journal associés
        blobs/           octets des imports, générations, extractions et mémoires
        logs/            journaux techniques bornés
        temp/            préparations, rendus de pages et segments audio
        backups/         sauvegardes gérées si espace suffisant
```

Les sous-dossiers exacts restent adaptables. Ce qui est contractuel est la séparation ressources applicatives/données, la résolution relative et la capacité de reconstruire les rattachements à partir de la base, conformément à [C10 et C11](contrats_transversaux.md).

Ne pas dupliquer les octets en créant physiquement un répertoire par conversation et par espace. Les dossiers utilisateur/mémoire de l’interface sont des vues de portée ; les blobs peuvent rester centralisés dans cette racine autonome.

## Chemins et identité après déplacement

| Élément | Valeur persistée | Valeur calculée au lancement ou à l’usage |
| :--- | :--- | :--- |
| Artefact | ID, hash contenu, chemin relatif sous données | Chemin résolu sous la racine courante, validé avant ouverture |
| Modèle embarqué | ID/checksum et chemin relatif sous modèles | Emplacement absolu du runtime courant |
| Conversation/espace | Identifiants et rattachements logiques | Aucun chemin de lettre de lecteur |
| Référence de preuve | ID source, page/plage/URL, version | Emplacement du contenu accessible |
| Secret de service | Référence de configuration non secrète | Valeur saisie par session ou fournie par stockage sécurisé local |

Tester espaces, accents, caractères non latins, nom de compte différent et nouvelle lettre de lecteur. Limiter volontairement les noms internes générés à des identifiants sûrs ; le nom original reste une métadonnée d’affichage et n’est pas concaténé librement au chemin de destination.

Un import doit produire une copie gérée. Après suppression du fichier original extérieur à Thot, la ressource importée doit encore être disponible ; sinon il s’agit d’un lien externe, pas d’une pièce jointe autonome.

## Mode strict, mode installé et supports non inscriptibles

- **Portable strict** : aucune écriture applicative automatique hors de `data/`, hors exceptions documentées du système/runtime. Cette règle ne couvre pas les actions utilisateur confirmées, comme un export vers une destination choisie ou la mise à jour explicite de l’application/modèles. Pas de bascule silencieuse vers AppData ou le répertoire utilisateur si la clé est pleine.
- **Mode installé optionnel** : données dans le répertoire utilisateur standard choisi par plateforme ; ce mode est explicitement différent et ne promet pas que copier le dossier des exécutables emporte les données.
- **Support en lecture seule** : refuser le démarrage avec écriture et proposer une copie vers un dossier inscriptible. Un futur mode consultation seule nécessitera une qualification séparée ; ne pas simuler silencieusement le mode portable.

Les profils de navigateur embarqué, caches de shaders, crash dumps ou mécanismes de l’OS peuvent laisser des traces externes. Les inventorier par plateforme et afficher les exceptions ; ne pas annoncer « aucune trace sur l’hôte » tant que cette exigence plus forte n’est pas testée.

## Profils de distribution à qualifier

| Profil proposé | Ce que Thot doit livrer | Prérequis/limites à vérifier | Test bloquant |
| :--- | :--- | :--- | :--- |
| Windows x64 CPU | Application, sidecar et bibliothèques natives compatibles, runtime web choisi | Version Windows, jeu d’instructions CPU, permissions et runtime WebView | Lancement hors ligne sur machine propre sans environnement développeur |
| Windows x64 accéléré | Même contrat plus runtime GPU redistribuable approprié | Pilote hôte et GPU réellement supportés par chaque modèle/runtime | Charge, inférence, swap, annulation et pic mémoire |
| Linux par cible qualifiée | Bundle adapté et dépendances explicitement traitées | Distribution/libc, WebKitGTK, bibliothèques système, montage exécutable | Machine propre sur la version minimale annoncée |
| macOS par architecture qualifiée | Bundle `.app`, sidecar et modèles adaptés | Version OS, Intel/Apple Silicon selon cible, validation de sécurité | Lancement d’un paquet téléchargé/déplacé, pas seulement construit localement |

Les détails amont et leurs sources sont rassemblés dans l’[annexe portabilité](annexes/08_portabilite_et_distribution.md). Cette matrice n’est pas une liste de plateformes déjà supportées ; les versions minimales sont volontairement non fixées tant qu’un paquet complet ne passe pas les tests.

## Choix de packaging et frontières

La cible de préproduction est un sidecar Python distribué avec ses dépendances par plateforme, en dossier, plutôt qu’une promesse de binaire universel. Prototyper la stratégie de worker isolé et le packaging ensemble : un worker qui fonctionne en développement n’est pas automatiquement relançable depuis le paquet distribué.

Pour Windows autonome, étudier l’embarquement local d’un runtime WebView2 Fixed Version, sa taille, sa licence et son chemin de profil. Une édition utilisant un runtime hôte Evergreen reste possible, mais n’a pas la même promesse « machine propre hors ligne » ; ne pas mélanger les deux dans le descriptif utilisateur.

Pour Linux, ne pas promettre qu’un fichier AppImage supprime toutes les contraintes de libc, de WebKit ou de montage. Pour macOS, qualifier la distribution et le lancement soumis aux contrôles de sécurité réels ; déplacer un bundle localement compilé ne remplace pas cet essai.

Ces propositions sont étayées et limitées dans les [ressources de packaging](annexes/08_portabilite_et_distribution.md). Le manifeste final doit identifier les composants inclus, les prérequis, l’état de signature et les licences, sans les déduire d’un simple nom de fichier.

## Modèles, mémoire et repli

Les modèles font partie du profil matériel, pas seulement des ressources statiques. Une distribution autonome inclut tous les poids auxiliaires nécessaires ; un fichier GGUF texte ne remplace pas un projecteur multimodal, un tokenizer ou l’encodeur du pipeline image.

L’édition légère peut demander un téléchargement initial explicite et vérifié par checksum. Elle ne sera pas étiquetée « entièrement utilisable hors ligne immédiatement » ; les fonctions indisponibles restent visibles comme indisponibles, sans téléchargement surprise.

La disponibilité d’un backend CPU ne prouve ni une vitesse interactive ni une mémoire suffisante pour toutes les phases. Qualifier séparément texte, OCR, vision, image et audio ; si un profil ne sait pas générer d’images, le dire plutôt que promettre une équivalence complète.

Pour mémoire unifiée, afficher RAM/mémoire système au lieu d’un chiffre trompeur de VRAM dédiée. La cible utilisateur « moins de 8 Go » reste une contrainte de profil à mesurer avec les poids, caches, buffers, contexte et composants du pipeline, non une règle inférée du nombre de paramètres actifs.

## Déplacement sûr, verrouillage et retrait de clé

Le déplacement/copiage supporté se fait application fermée, tous les workers arrêtés et toutes les écritures terminées. L’application offre une action « Préparer le déplacement » : arrêter les jobs ou les annuler, attendre les workers, fermer la base selon son mode de journalisation et confirmer que les données sont quiescentes.

Ne pas débrancher le support pendant une génération, une sauvegarde ou une migration. En cas de retrait brutal, le prochain lancement entre en récupération : vérifier cohérence de la base et des ressources, signaler les manques et préserver les données restantes ; aucune garantie ne peut empêcher une perte due à un support défaillant.

Un verrou applicatif par racine de données interdit deux instances écrivant simultanément sur le même dossier. Ne pas supprimer un verrou en se fondant seulement sur un PID enregistré sur une autre machine ; utiliser les primitives de verrouillage et une procédure de récupération adaptée au support.

L’utilisation d’une base active à travers un partage réseau ou un dossier synchronisé n’est pas supportée par le profil initial. Pour transporter/synchroniser, fermer l’application et copier une sauvegarde cohérente, pas une sélection improvisée des fichiers.

## Sauvegardes, journaux et espace libre

La sauvegarde applicative doit couvrir base cohérente, blobs référencés, paramètres non secrets, manifestes et versions de schéma. Les poids peuvent être inclus pour une sauvegarde autonome ou exclus explicitement avec leurs identifiants/checksums.

Ne jamais copier uniquement le fichier `.db` d’une base active en présumant que les autres fichiers n’importent pas. Choisir une procédure de sauvegarde et de journalisation testée sur le support ; ne pas effacer les fichiers de journal pour « nettoyer la clé ».

Vérifier l’espace disponible avant import, génération, extraction, migration et sauvegarde, en tenant compte du fichier final et des temporaires coexistants. Le besoin dépend du modèle et de la tâche ; l’interface ne doit pas annoncer une taille fixe déduite de celle de l’exécutable.

Le format du support impose ses propres limites de taille de fichier et de fiabilité. Vérifier notamment les gros poids GGUF avant de promettre une clé prête à copier ; consulter les [sources filesystem et SQLite](annexes/08_portabilite_et_distribution.md).

## Mises à jour et migration

Les mises à jour ne doivent pas écraser `data/` ni les modèles personnels. Avant migration de schéma, sauvegarder, vérifier version et espace, exécuter la migration hors des runs, puis vérifier ouverture des données.

Une ancienne version doit refuser proprement une base plus récente non supportée. Le retour arrière signifie restaurer ensemble ancien exécutable et sauvegarde compatible, pas réouvrir arbitrairement une base migrée.

La mise à jour de modèles et celle de l’application sont distinctes. Enregistrer l’identité exacte des versions ayant produit un artefact ; un changement de modèle ne réécrit pas les anciennes preuves.

## Accessibilité du premier lancement

Un écran de diagnostic explique ce qui est utilisable sans demander un terminal : profil choisi, modèles disponibles, mémoire estimée/mesurée, fonctions non supportées et actions de résolution. Toute réduction de contexte ou de qualité doit être indiquée et acceptée.

Le parcours clavier couvre démarrage, import, chat, annulation, historique, lecture des erreurs, export et préparation du déplacement. Ne pas rendre le micro ou une icône sans libellé indispensable au fonctionnement.

Les langues de l’interface, du modèle texte, de l’OCR et des voix sont des capacités séparées. Tester le français effectivement retenu, au lieu de présumer qu’un runtime multilingue rend tous ses checkpoints équivalents.

## Acceptation et preuve de portabilité

Une livraison portable n’est acquise qu’après une matrice d’essais sur paquets construits, hors environnement de développement. Les [tests T-P01 à T-P12](tests_acceptation.md) couvrent première exécution, déplacement, dépendances, fermeture, récupération, formats de support et changement de versions.

À conserver pour chaque qualification : manifeste, checksums, configuration hôte, protocole d’essai, mesures, résultat et écarts. La présente révision documentaire n’a exécuté aucun de ces tests matériels ; elle rend leur existence obligatoire avant annonce de compatibilité.
