# Annexe 06 — Pratiques Python : sidecar, IPC, persistance

Sources récupérées le **2026-09-06**. Conventions **Fait / Décision Thot / À mesurer** : voir le [sommaire des annexes](README.md).

## Sous-processus et flux : ce que la documentation impose

**Fait.** La création de sous-processus asyncio applique par défaut une **limite de tampon de 65 536 octets (64 KiB)** aux enveloppes de flux des pipes ([Python — asyncio subprocess](https://docs.python.org/3/library/asyncio-subprocess.html)).

**Fait.** L'attente de fin de processus **peut interbloquer** lorsque la sortie standard ou d'erreur est un pipe et que l'enfant produit assez de données pour remplir le tampon du système ; la documentation recommande d'utiliser la méthode de communication plutôt que de manipuler directement les flux ([Python — asyncio subprocess](https://docs.python.org/3/library/asyncio-subprocess.html)).

**Fait.** Cette même méthode de communication **met toutes les données en mémoire** et la documentation avertit de ne pas l'employer pour des volumes importants ou illimités ([Python — asyncio subprocess](https://docs.python.org/3/library/asyncio-subprocess.html)).

**Fait.** Si l'objet processus est collecté par le ramasse-miettes alors que le processus tourne encore, **l'enfant est tué** ([Python — asyncio subprocess](https://docs.python.org/3/library/asyncio-subprocess.html)).

**Fait.** Sous Windows, seule la boucle d'événements par défaut prend en charge les sous-processus ; l'autre implémentation citée n'offre aucun support ([Python — asyncio subprocess](https://docs.python.org/3/library/asyncio-subprocess.html)).

**Décision Thot.** Ces contraintes fixent le contrat IPC de [C08](../contrats_transversaux.md) :

- `stdout` porte uniquement les lignes JSON de contrôle, `stderr` les journaux ; les deux flux sont **consommés en continu et séparément**, jamais par une attente de fin ni par une lecture unique qui bufferiserait tout un run.
- Les tailles de lignes et de files sont bornées et alignées sur la limite de tampon, avec backpressure explicite plutôt que croissance mémoire non bornée.
- Les corps volumineux (documents, audio, images) passent par référence de fichier ou canal dédié, jamais par le bus de contrôle.
- La durée de vie du processus sidecar est détenue par un objet référencé de manière stable, pour éviter une terminaison provoquée par le ramasse-miettes.
- Le comportement de la boucle d'événements est **spécifique à Windows** : la qualification de plateforme le vérifie explicitement.

**À mesurer / `n.a.`** : noms exacts des implémentations de boucle concernées — non repris ici, faute d'extrait récupéré ; débit soutenable du bus JSON Lines ; latence d'annulation observée bout en bout.

## Empaquetage du sidecar

**Fait.** En mode fichier unique, l'amorceur PyInstaller **extrait les fichiers de support dans un dossier temporaire** du système avant d'exécuter le programme, ce qui rend le démarrage un peu plus lent ; le dossier est supprimé en sortie, **sauf en cas de plantage ou d'arrêt forcé**, ce qui laisse des dossiers résiduels ([PyInstaller — Operating mode](https://pyinstaller.org/en/stable/operating-mode.html)).

**Fait.** Ce mode est **incompatible avec un répertoire temporaire monté sans droit d'exécution**, ne préserve pas les attributs de fichiers, et son emplacement d'extraction dépend d'options ou de variables d'environnement ([PyInstaller — Operating mode](https://pyinstaller.org/en/stable/operating-mode.html), [PyInstaller — Usage](https://pyinstaller.org/en/stable/usage.html)).

**Fait.** Le mode dossier est recommandé en premier pour le diagnostic, et permet de remplacer l'exécutable sans redistribuer les dépendances identiques ([PyInstaller — Operating mode](https://pyinstaller.org/en/stable/operating-mode.html)).

**Fait.** La sortie est **spécifique au système d'exploitation, à la version de Python et à la largeur de mot** : pour préparer une distribution destinée à un autre système, il faut exécuter l'outil **sur ce système** ([PyInstaller — Operating mode](https://pyinstaller.org/en/stable/operating-mode.html)).

**Décision Thot.** Le sidecar est distribué **en dossier**, pas en fichier unique. C'est cohérent avec la promesse « dossier portable » du [contrat de portabilité](../portabilite.md) : pas d'extraction dans le répertoire temporaire de l'hôte, pas de résidus après arrêt brutal, diagnostic possible sur la machine de l'utilisateur. La révision précédente de cette fiche recommandait le mode fichier unique et une collecte globale de dépendances : recommandation retirée. Corollaire assumé : **une chaîne de construction par plateforme et architecture**, et un manifeste indiquant OS, architecture, version de Python et empreintes ([C19](../contrats_transversaux.md)).

## Persistance : sessions et intégrité

**Fait.** Une session SQLAlchemy est un objet **mutable et à état**, représentant **une transaction**, et **ne peut pas être partagée entre threads ou tâches concurrentes** sans synchronisation soignée ; le modèle recommandé est une session par thread, une session asynchrone par tâche, avec des utilitaires de portée thread-local ou contextuelle lorsque le passage explicite est impraticable ([SQLAlchemy — Session Basics](https://docs.sqlalchemy.org/en/20/orm/session_basics.html)).

**Fait.** Sous SQLite, les contraintes de clés étrangères **sont sans effet par défaut** : elles exigent une version suffisante de la bibliothèque, une compilation ne les omettant pas, et l'émission du pragma correspondant **sur toutes les connexions avant usage**, ce que SQLAlchemy permet via un écouteur d'événement de connexion ([SQLAlchemy — dialecte SQLite](https://docs.sqlalchemy.org/en/20/dialects/sqlite.html)).

**Décision Thot.** Deux règles en découlent :

1. les écritures de la base passent par un **écrivain unique** alimenté par une file bornée, avec une session par thread ou par tâche — jamais une session partagée entre l'orchestrateur, les workers d'inférence et la maintenance ;
2. l'intégrité référentielle n'est **jamais présumée** : le pragma est posé à chaque ouverture de connexion, et son effet est vérifié par un test d'acceptation, y compris après déplacement du dossier ([tests_acceptation.md](../tests_acceptation.md)).

Les aspects propres au support amovible (journal WAL, sauvegardes, systèmes de fichiers) sont traités dans l'[annexe 08](08_portabilite_et_distribution.md).

**À mesurer** : contention observée sur l'écrivain unique en charge réelle ; coût des transactions courtes de publication ([C11](../contrats_transversaux.md)) ; comportement du pragma et des migrations sur support lent.
