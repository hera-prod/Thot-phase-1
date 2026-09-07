# Annexe 08 — Portabilité et distribution

Sources récupérées le **2026-09-06**. Conventions **Fait / Décision Thot / À mesurer** : voir le [sommaire des annexes](README.md). Cette fiche étaye le [contrat de portabilité](../portabilite.md) ; elle ne déclare aucune plateforme supportée.

## Ce que Tauri produit réellement

**Fait.** Sous Windows, Tauri produit un **installateur `.msi`** (chaîne WiX, construction sous Windows uniquement) et un **installateur `.exe`** (NSIS) ; la page de distribution ne documente **aucune sortie « application portable »**, ni exécutable autonome, ni dossier ([Tauri — Windows Installer](https://v2.tauri.app/distribute/windows-installer/)).

**Fait.** Les applications Tauri « utilisent le webview de l'OS », via la couche d'abstraction WRY ([Tauri — Architecture](https://v2.tauri.app/concept/architecture/)). La correspondance par plateforme est documentée côté WRY : **WebView2 fourni par Microsoft Edge Chromium** sous Windows, **WebKit natif** sous macOS, **WebKitGTK** sous Linux ([dépôt tauri-apps/wry](https://github.com/tauri-apps/wry)).

**Décision Thot.** Le « dossier portable » attendu par l'utilisateur n'est donc pas un format livré par l'outil : il doit être **construit par le projet** (arborescence, lanceur, résolution de racine relative), en s'appuyant sur les artefacts de construction mais sans présumer qu'un installateur suffit. Aucune promesse de portabilité ne peut se fonder sur la seule documentation Tauri.

## Windows : le runtime WebView2 est le point dur

**Fait.** La configuration Tauri offre plusieurs modes d'installation du webview, avec les surcoûts annoncés : amorceur téléchargé (défaut, réseau requis, +0 Mo), amorceur embarqué (+~1,8 Mo, réseau requis), installateur hors-ligne (+~127 Mo), **version fixe (+~180 Mo, sans réseau)** et mode « ignorer » déconseillé ([Tauri — Windows Installer](https://v2.tauri.app/distribute/windows-installer/)).

**Fait.** Dans le mode **Fixed Version** de Microsoft, le runtime est téléchargé, décompressé, **inclus dans le paquet de l'application et expédié avec elle** ; le développeur **contrôle et doit assurer les mises à jour**, le runtime chez le client **n'étant pas mis à jour automatiquement**. Microsoft indique que ces binaires ajoutent **plus de 250 Mo** au paquet, qu'aucune clé de registre n'est créée, et que la décompression via l'Explorateur est déconseillée ([Microsoft — Distribute a WebView2 app](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/distribution)).

**Fait.** Deux contraintes bloquantes sont documentées pour ce mode : il **ne peut pas être exécuté depuis un emplacement réseau ou un chemin UNC**, et sous **Windows 10**, à partir de la version 120 du runtime, une application Win32 non packagée doit accorder des droits d'accès aux identités de packages d'application sur le dossier du runtime ([Microsoft — Distribute a WebView2 app](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/distribution)).

**Fait.** Le mode **Evergreen** repose sur un runtime **partagé et mis à jour automatiquement** par Microsoft ; une application ne peut pas imposer une version précise à l'échelle de la machine ([Microsoft — Distribute a WebView2 app](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/distribution)).

**`n.a.`** Les conditions de licence et de redistribution du runtime ne figurent pas sur cette page : elles doivent être vérifiées séparément avant toute annonce d'édition autonome.

**Décision Thot.** L'édition « machine propre, hors ligne » implique le runtime en **version fixe embarquée**, avec son poids (> 250 Mo), sa charge d'entretien de sécurité et ses droits d'accès sous Windows 10. Deux conséquences pour le [contrat de portabilité](../portabilite.md) : la documentation utilisateur ne mélange pas cette édition avec une édition reposant sur le runtime de l'hôte, et un dossier Thot placé sur un **partage réseau ou un chemin UNC** est déclaré **non supporté** dans cette configuration.

## Sidecar : un binaire par cible

**Fait.** Les binaires externes sont déclarés dans la configuration du bundle et Tauri les recherche sous la forme `nom{-triplet-cible}{.extension}` (par exemple avec les triplets Windows MSVC, Linux GNU ou Apple selon l'architecture) ; **un binaire doit être fourni pour chaque plateforme visée** ([Tauri — Sidecar](https://v2.tauri.app/develop/sidecar/), [Tauri — Référence de configuration](https://v2.tauri.app/reference/config/)).

**Fait.** L'exécution du sidecar depuis le frontend exige une **permission explicite** dans les capacités de l'application ; l'outil d'ajout automatique du triplet cible ne fonctionne pas en compilation croisée ([Tauri — Sidecar](https://v2.tauri.app/develop/sidecar/)).

**Fait.** Du côté Python, la sortie d'empaquetage est **spécifique au système, à la version de Python et à la largeur de mot** : pour cibler un autre système, l'outil doit être exécuté **sur ce système** ([PyInstaller — Operating mode](https://pyinstaller.org/en/stable/operating-mode.html)). Les compromis mode dossier / mode fichier unique sont détaillés en [annexe 06](06_bonnes_pratiques_python.md).

**Décision Thot.** La cible reste explicitement **une distribution par plateforme et architecture**, jamais un binaire universel, avec un manifeste déclarant OS, architecture, versions et empreintes ([C19](../contrats_transversaux.md)).

## Linux

**Fait.** Tauri v2 requiert les paquets de développement **WebKitGTK 4.1** et recommande de construire sur le **plus ancien système cible** les fournissant, en citant Ubuntu 22.04 et Debian 12 ; construire sur un système plus récent **relève la version minimale de glibc requise**, avec une erreur de chargement caractéristique à l'exécution sur les machines plus anciennes ([Tauri — AppImage](https://v2.tauri.app/distribute/appimage/)).

**Fait.** La page des prérequis liste des dépendances par distribution **sans fixer de version minimale** de distribution ni de glibc ([Tauri — Prerequisites](https://v2.tauri.app/start/prerequisites/)).

**Fait.** Un AppImage **embarque ses dépendances**, s'exécute **sans installation** après avoir été rendu exécutable, mais sa taille passe d'environ 2 à 6 Mo à **plus de 70 Mo**, et les bibliothèques cœur telles que glibc **cassent fréquemment la compatibilité** avec les systèmes plus anciens ([Tauri — AppImage](https://v2.tauri.app/distribute/appimage/)).

**`n.a.`** Le besoin éventuel de FUSE côté hôte n'est pas mentionné sur cette page.

**Décision Thot.** L'image de construction Linux est figée et documentée, la glibc minimale **constatée** est publiée dans le manifeste, et le [contrat de portabilité](../portabilite.md) continue de ne promettre aucune indépendance totale vis-à-vis de la libc, de WebKit ou du montage du support.

## macOS

**Fait.** Le paquet `.app` est un **répertoire** contenant exécutable, ressources et métadonnées, produit **sur un Mac** ; il cible par défaut **macOS 10.13 et supérieur**, valeur configurable ([Tauri — macOS Application Bundle](https://v2.tauri.app/distribute/macos-application-bundle/)).

**Fait.** La signature exige un **compte Apple Developer payant (99 USD par an)** et un appareil Apple ; le certificat de diffusion hors App Store est de type **Developer ID Application** ; sans signature, une application téléchargée peut être signalée comme endommagée et refuser de démarrer ([Tauri — Code signing macOS](https://v2.tauri.app/distribute/sign/macos/)).

**Fait.** Le service de notarisation d'Apple analyse un logiciel signé Developer ID, émet un **ticket** publié en ligne ou agrafé au paquet, que **Gatekeeper** consulte au lancement ; le **Hardened Runtime est exigé** par le service pour les applications et les cibles en ligne de commande ([Apple — Notarizing macOS software before distribution](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution)).

**Décision Thot.** Compte, signature et notarisation sont des **prérequis budgétaires et calendaires** de la qualification macOS, à traiter avant toute annonce. L'essai bloquant reste le lancement d'un paquet **téléchargé puis déplacé**, pas d'un paquet construit localement.

## SQLite sur support amovible

**Fait.** En mode WAL, la base est accompagnée d'un fichier de journal et d'un fichier de mémoire partagée ; à la fermeture de la **dernière** connexion, un **checkpoint final** est exécuté puis ces fichiers sont **supprimés**, sauf terminaison sans fermeture propre ou option de persistance explicite ([SQLite — Write-Ahead Logging](https://www.sqlite.org/wal.html)).

**Fait.** Le fichier de journal **fait partie de l'état persistant** de la base : il doit être déplacé ou copié avec elle, sous peine de **perdre des transactions validées ou de corrompre** la base ; la seule manière sûre de le supprimer est d'ouvrir puis de fermer la base par l'interface de programmation ([SQLite — Write-Ahead Logging](https://www.sqlite.org/wal.html)).

**Fait.** Le mode WAL **ne fonctionne pas sur un système de fichiers réseau**, tous les processus devant partager la même machine ([SQLite — Write-Ahead Logging](https://www.sqlite.org/wal.html)).

**Fait.** Depuis la version 3.22.0 (2018), la lecture d'une base WAL sur média en lecture seule est possible sous conditions ; la bonne pratique documentée avant gravure sur support non inscriptible est de repasser en mode de journalisation par suppression ([SQLite — Write-Ahead Logging](https://www.sqlite.org/wal.html)).

**Fait.** Les méthodes de sauvegarde sûres sur base vive sont l'**interface de sauvegarde incrémentale** — qui produit un instantané identique bit à bit — ainsi que l'aspiration vers un nouveau fichier ou l'outil de synchronisation dédié ; une copie par le système de fichiers pendant une transaction peut mélanger ancien et nouveau contenu et produire une sauvegarde corrompue ([SQLite — Backup API](https://www.sqlite.org/backup.html), [SQLite — How To Corrupt](https://www.sqlite.org/howtocorrupt.html)).

**Fait.** Les clés USB **peuvent mentir sur l'achèvement des synchronisations** : la validation revient alors que le témoin lumineux clignote encore, et **retirer la clé pendant ce clignotement corrompt fréquemment la base** ; certains contrôleurs peuvent même endommager des fichiers non ouverts en cas de coupure pendant une écriture ([SQLite — How To Corrupt](https://www.sqlite.org/howtocorrupt.html)).

**Fait.** La même page indique qu'en mode WAL, une synchronisation défaillante ne peut corrompre le fichier que **pendant un checkpoint**, un échec au moment de la validation coûtant la durabilité et non l'intégrité ; désactiver la synchronisation expose en revanche à la corruption, et le réglage par défaut est recommandé ([SQLite — How To Corrupt](https://www.sqlite.org/howtocorrupt.html)).

**Décision Thot.** L'action « Préparer le déplacement » du [contrat de portabilité](../portabilite.md) est donc définie par des vérifications observables : arrêt des workers, **fermeture de toutes les connexions** pour provoquer le checkpoint final, puis **contrôle de l'absence des fichiers annexes** avant de déclarer les données quiescentes. Les sauvegardes n'utilisent jamais une copie du seul fichier de base. L'usage d'une base active sur partage réseau ou dossier synchronisé reste hors profil, ce que la documentation SQLite confirme pour le mode WAL.

## Limites du système de fichiers du support

**Fait.** La taille maximale d'un fichier est de **4 Gio en FAT32** et de **2^64−1 octets en exFAT comme en NTFS** ; la **journalisation des métadonnées** est présente en NTFS mais **absente en exFAT et FAT32**, de même que les fichiers creux et les liens ([Microsoft — File system functionality comparison](https://learn.microsoft.com/en-us/windows/win32/fileio/filesystem-functionality-comparison)).

**Décision Thot.** Le manifeste de distribution et le diagnostic de premier lancement doivent **détecter le système de fichiers** de la racine de données. Conséquence concrète : sur **FAT32**, un poids de modèle dépassant 4 Gio est **impossible à copier** — ce qui concerne directement les quantifications de LFM2.5-8B-A1B à partir de Q4_K_M (≈ 5,16 Go, voir [annexe 02](02_quantification_gguf.md)). exFAT lève cette limite mais **n'apporte aucune journalisation** : le risque lié à un retrait brutal demeure entier, et l'interface doit l'énoncer plutôt que promettre une clé « prête à copier ».

**À mesurer** : poids total de chaque édition et espace libre requis avec temporaires ; comportement de récupération après retrait brutal sur FAT32, exFAT et NTFS ; durée réelle de l'action « Préparer le déplacement » ; version minimale d'OS et de glibc effectivement produite par chaque chaîne de construction. Ces essais correspondent à la matrice de tests de portabilité de [tests_acceptation.md](../tests_acceptation.md) ; aucun n'a été exécuté au titre de la présente révision documentaire.
