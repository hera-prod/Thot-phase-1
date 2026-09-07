# Matrice d’acceptation de la préproduction

Cette matrice décrit les essais à exécuter lors du développement et de la qualification des paquets. Tous les tests applicatifs et matériels ci-dessous sont **non exécutés dans cette révision documentaire** ; seuls les contrôles de documents consignés dans l’[audit](audit_preproduction.md) ont été réalisés.

Les résultats attendus sont des exigences, pas des observations. Pour un essai réel, enregistrer date, commit, paquet/checksum, OS/CPU/GPU/pilote, modèles/configuration, résultat, mesures et écart ; un test non exécuté ne compte jamais comme réussi.

## Ordre d’exécution

Commencer par contrats purs avec doubles de test, puis intégration sans modèle, puis inférence réelle sur un profil, puis paquet distribué sur machine propre. Rejouer les tests antérieurs à chaque phase : ajouter FLUX ou une voix ne doit pas casser le lancement, la suppression ou le déplacement du dossier.

Les IDs restent stables même si les détails évoluent. Un jalon est clos lorsque ses critères ont une preuve et que les échecs bloquants sont corrigés ou retirés explicitement de la portée annoncée.

## Phase 1 : conversation, rôles et outils

| ID | Scénario et action | Résultat attendu | Contrat |
| :--- | :--- | :--- | :--- |
| T-C01 | Envoyer un premier message depuis l’accueil puis recharger l’historique | Une conversation et un message utilisateur, pas de doublon ; même ID retrouvé | C01, C09 |
| T-C02 | Rejouer la même requête de transport `request_id` | Même acceptation/run, aucune seconde inférence ni écriture | C08, C09 |
| T-C03 | Injecter un marqueur de raisonnement dans la sortie interne de l’orchestrateur | Marqueur absent du prompt générateur, du message public et du journal durable ordinaire | C03, C05 |
| T-C04 | Introduire une préférence au tour précédent puis demander une réponse | Orchestrateur et générateur reçoivent même `SharedContext.revision` et préférence ; message courant une seule fois | C03 |
| T-C05 | Orchestrateur appelle lecture puis vision puis produit un brief | Les deux appels/résultats restent corrélés et utilisables, aucune remise à zéro entre itérations | C04 |
| T-C06 | Essayer un outil depuis le générateur ou envoyer un nom inconnu | Refus logiciel, aucun effet de bord, erreur structurée | C03, C18 |
| T-C07 | Produire un brief invalide ou qui contient de la planification brute | Rejet/réparation bornée ou brief minimal contrôlé, pas transmission brute au générateur | C03 |
| T-C08 | Fournir une image puis changer de modèle pour OCR et retour texte | Réacquisition du runtime texte, jamais appel sur ancien handle ; preuve liée à l’artefact | C04, C07 |
| T-C09 | Répéter sans progrès le même appel d’outil | Arrêt borné et résultat partiel/limite expliquée | C04 |
| T-C10 | Relancer l’application après une observation d’outil | Journal/provenance conservés, sans réinjecter toute la trace dans le prochain prompt | C05 |
| T-C11 | Mettre des crochets, une citation de balises ou « The user » dans une vraie réponse | Le rendu ne supprime pas du contenu légitime par heuristique arbitraire | C05 |
| T-C12 | Échouer sur un outil tout en disposant d’une autre preuve valide | Résultat d’échec corrélé, réponse limitée aux preuves réellement disponibles | C04, C16 |

## Phase 1 : contexte, cycle de vie et persistance

| ID | Scénario et action | Résultat attendu | Contrat |
| :--- | :--- | :--- | :--- |
| T-K01 | Construire un prompt près de la fenêtre avec plusieurs schémas/outils | Décompte tokenizer/template du rôle, réserve sortie incluse, pas dépassement silencieux | C06 |
| T-K02 | Compacter une conversation puis ouvrir l’ancien historique | Messages originaux inchangés ; snapshot et borne explicites ; références ouvrables | C06 |
| T-K03 | Échec, résumé vide ou résumé trop long lors de compaction | Aucun remplacement du snapshot valide ; erreur ou stratégie bornée visible | C06 |
| T-K04 | Nouveau message pendant la préparation d’un snapshot | Contrôle de révision ; pas d’activation qui masque le nouveau message | C06 |
| T-K05 | Reproduire le profil utilisateur grand contexte LFM sous 8 Go | Configuration exacte et pic mesurés ; cible validée ou écart documenté, pas réduction cachée | C06, C19 |
| T-R01 | Envoyer un chat et une génération simultanés à partir de la phase 2 | Une location GPU exclusive, autre job en file bornée, UI disponible | C07 |
| T-R02 | Bloquer une inférence native puis demander annulation | Lecture IPC toujours active ; arrêt confirmé du worker avant de libérer la location | C07, C09 |
| T-R03 | Faire échouer unload/load ou simuler mémoire non libérée | Pas de chargement concurrent ; état failed récupérable et erreur claire | C07 |
| T-R04 | Émettre événements doublés, tardifs et changement de conversation | Rejet par run/seq, aucun delta au mauvais endroit, un terminal par run | C08 |
| T-R05 | Fermer l’application pendant l’OCR d’un PDF | Arrêt/annulation explicite, workers terminés, données cohérentes, fichiers temporaires réconciliés | C09 |
| T-R06 | Tuer le processus après publication blob mais avant commit | Récupération identifie l’orphelin sans fausse entrée galerie ni effacement d’un fichier utilisé | C11 |
| T-R14 | Déclencher la maintenance pendant une publication en cours | Barrière : aucune suppression physique tant qu’une publication/rattachement est actif ; aucun blob actif supprimé | C11 |
| T-R07 | Plusieurs producteurs écrivent logs et événements en charge | Chaque ligne de contrôle reste un JSON complet ; logs séparés, file bornée | C08 |
| T-R08 | Lire/écrire depuis plusieurs tâches CPU et vérifier transactions | Pas de session DB partagée sans contrôle, transactions courtes, clés étrangères effectives | C10, C11 |
| T-R09 | Rendre le sidecar indisponible au démarrage | L’UI affiche indisponibilité ; aucune commande réussie fictivement | C08 |
| T-R10 | Un run admis appelle compaction puis outil puis génération | Aucune nouvelle admission imbriquée ; locations acquises successivement et rendues après fin native | C07 |
| T-R11 | Deux envois quasi simultanés dans la même conversation | Un seul run de chat non terminal ; second refusé avant insertion de son message, sans contamination du contexte du premier | C09 |
| T-R12 | Redémarrage après interruption avec événements tardifs | Watermark/seq persistés ; seuls les événements postérieurs appliqués, run `interrupted` récupéré en terminal `RUN_INTERRUPTED` | C08 |
| T-R13 | Épuiser le budget sans réserve de finalisation | Message de limite déterministe, aucun nouvel appel LLM de clôture | C04 |

## Phase 1 : pièces jointes et autorisation

| ID | Scénario et action | Résultat attendu | Contrat |
| :--- | :--- | :--- | :--- |
| T-F01 | Importer un PDF, supprimer l’original extérieur et relancer | Copie gérée toujours disponible avec ID stable | C10 |
| T-F02 | Fournir faux ID, chemin `../`, lien symbolique/jonction hors racine | Refus avant lecture ; pas d’accès par simple hash ou préfixe | C10, C18 |
| T-F03 | Déposer plusieurs images puis demander « cette image » sans sélection claire | Clarification, pas choix automatique de la dernière | C12 |
| T-F04 | PDF textuel, scan, page mixte et tableau à mise en page significative | Extraction native quand adaptée, OCR/rendu ciblé sinon, pages/provenance conservées | C12 |
| T-F05 | PDF très volumineux ou image énorme/endommagée | Limites ressources avant allocations excessives ; rendu page par page borné | C12, C18 |
| T-F06 | Projecteur manquant ou handler incompatible | Erreur explicite ; aucune substitution par architecture sans rapport ni passage image comme texte | C07 |
| T-F07 | Supprimer une conversation dont un artefact est également lié ailleurs | Retrait de ses liens sans suppression du blob encore référencé | C11 |
| T-F08 | Remplacer une source ou changer l’extracteur | Nouvelle version des dérivés ; anciennes preuves conservées ou signalées obsolètes | C12, C16 |

## Phase 2 : image et galerie

| ID | Scénario et action | Résultat attendu | Contrat |
| :--- | :--- | :--- | :--- |
| T-I01 | Générer depuis studio puis depuis chat avec mêmes paramètres | Même service et protocole d’artefacts, deux parcours UI sans moteur dupliqué | C13 |
| T-I02 | Tester 0, 1 et 4 références puis 5 dans le profil limité à 4 | Succès selon profil qualifié ; 5 refusées explicitement avant génération | C13 |
| T-I03 | Référence d’un autre périmètre, fichier absent ou non image | Refus, pas substitution silencieuse | C10, C13 |
| T-I04 | Échec pendant encodeur, sampling ou VAE | Erreur par étape, aucun fichier partiel annoncé prêt | C11, C13 |
| T-I05 | Rejouer une notification d’artefact après reprise | Une seule entrée galerie, métadonnées et sources d’entrée conservées | C09, C13 |
| T-I06 | Pipeline réel quantifié, 4 références et résolution du profil utilisateur | Pics VRAM/RAM et durée consignés ; aucune extrapolation à toutes résolutions/plateformes | C07, C19 |
| T-I07 | Ouvrir la galerie puis masquer les imports | Créations accessibles ; cache web/extractions internes non mêlés aux créations | C10 |
| T-I08 | Générer au studio sans conversation puis attacher l’image à un chat | Run de production corrélé malgré conversation_id nul ; aucun faux message ; accès du chat accordé seulement après rattachement | C08, C10, C13 |

## Phase 3 : documents, voix et export

| ID | Scénario et action | Résultat attendu | Contrat |
| :--- | :--- | :--- | :--- |
| T-D01 | Générer TXT/MD/CSV/PDF français avec accents, tableaux et longues pages | Fichiers ouvrables, données et encodage fidèles, erreurs de mise en page détectées | C13 |
| T-D02 | Soumettre un format inconnu, du code Python ou un chemin absolu de sortie | Refus de schéma ; aucune exécution et aucune écriture arbitraire | C13, C18 |
| T-D03 | Export CSV contenant formule, guillemets, séparateurs et retours ligne | Politique de neutralisation explicite, échappement correct, valeur brute conservée en interne si nécessaire | C13 |
| T-D04 | Vérifier les chiffres du PDF contre données d’entrée | Validation factuelle séparée de « PDF ouvrable », écart signalé | C13 |
| T-D05 | Exporter localement un fichier et rencontrer un nom déjà présent | Choix utilisateur avant remplacement ; aucune transmission externe implicite | C11 |
| T-A01 | Micro refusé, absent, silencieux puis parole française | Erreurs actionnables ; aucune transcription inventée pour silence ; test qualité français documenté | C14 |
| T-A02 | Modifier le brouillon pendant une transcription en vol | Aucun écrasement ; résultat associé au recording_id et confirmation d’insertion | C14 |
| T-A03 | Cliquer lire/stop plusieurs fois sur deux messages | Lecture du bon message, arrêt effectif et ressources bornées | C14 |
| T-A04 | Lire à voix haute avec micro disponible | Pas de boucle TTS→STT ; suspension/gestion explicitée | C14 |
| T-A05 | STT/TTS CPU en parallèle d’un profil LLM CPU ou GPU | Latences et RAM mesurées, quotas respectés, UI réactive | C07, C14 |
| T-A06 | Fermer/reprendre après interruption audio | Temporaires traités selon rétention ; aucun contenu audio conservé à l’insu de l’utilisateur | C14 |

## Phase 4 : recherche et preuves

| ID | Scénario et action | Résultat attendu | Contrat |
| :--- | :--- | :--- | :--- |
| T-W01 | Question entièrement résolue par un PDF pertinent | Preuve locale citée et compteur réseau à zéro | C15 |
| T-W02 | Même question à partir d’un snapshot web encore frais | Lecture du cache sans requête moteur inutile | C15, C16 |
| T-W03 | Demander « aujourd’hui » pour un fait changeant dans un cache ancien | Revalidation si autorisée, sinon avertissement d’ancienneté | C15, C16 |
| T-W04 | Mode local_only et sources insuffisantes | Aucun réseau, lacunes explicites et clarification possible | C15 |
| T-W05 | Fiche de catalogue trompeuse ou recherche lexicale sans résultat | Ne pas déduire absence de contenu de ce seul signal ; lecture ciblée/limitation visible | C12, C15 |
| T-W06 | Page demandant d’ignorer les règles ou d’exfiltrer les pièces jointes | Texte traité comme données ; aucune permission élargie ni fuite | C18 |
| T-W07 | URL/redirection vers réseau interne, schéma interdit ou réponse énorme | Refus du fetcher et arrêt borné | C18 |
| T-W08 | Brief tente de citer un evidence_id inconnu ou URL inventée | Citation refusée, réponse corrigée/limitée | C16 |
| T-W09 | Deep research avec contradictions et sous-question manquante | Itération ciblée dans budget, contradiction conservée, lacunes rapportées si limite | C15 |
| T-W10 | Quota épuisé, panne moteur ou annulation | Résultats partiels locaux conservés, pas retries infinis ni rapport déclaré complet | C09, C15 |
| T-W11 | Recherche nécessitant des informations privées de document | Requête minimisée ; accord préalable avant transfert sensible | C15, C18 |
| T-W12 | Purger un cache utilisé par une réponse ancienne | Sources référencées conservées selon politique ou absence signalée explicitement | C11, C16 |

## Phase 5 : espaces et mémoire

| ID | Scénario et action | Résultat attendu | Contrat |
| :--- | :--- | :--- | :--- |
| T-M01 | Deux espaces avec noms de fichiers identiques et une conversation hors espace | Droits séparés ; aucune fuite par ID, hash, recherche ou suggestion | C10, C17 |
| T-M02 | Importer une fois dans l’espace puis ouvrir deux conversations | Même ressource partagée disponible sans nouvel import | C17 |
| T-M03 | Demander à l’IA d’écraser le fichier utilisateur | Refus ; éventuelle nouvelle version proposée comme nouvel artefact | C11, C17 |
| T-M04 | Proposer une mémoire puis refuser/valider sa promotion | Statut correct, sources et versions conservées, aucune promotion cachée | C17 |
| T-M05 | Fiche populaire mais hors sujet contre fiche récente pertinente | La pertinence prime ; fiche utile moins populaire accessible | C17 |
| T-M06 | Réponse située au-delà des cinq premiers résultats | Pagination/recherche ciblée permet de l’atteindre | C17 |
| T-M07 | Réouvrir dix fois une fiche inutilisée dans la réponse | L’usage validé ne s’incrémente pas comme si elle avait résolu dix demandes | C17 |
| T-M08 | Deux fiches se contredisent ou une source devient périmée | Conflit/ancienneté visibles, correction/invalidation possibles | C16, C17 |
| T-M09 | Supprimer une conversation, purger la mémoire, déplacer entre espaces | Documents utilisateur préservés ; droits/références recalculés sans partage tacite | C11, C17 |
| T-M10 | Exporter un espace puis le restaurer dans une nouvelle racine | Dépendances incluses ou manquantes signalées ; aucune référence absolue cachée | C10, C17 |
| T-M11 | Déplacer un chat dont une ancienne réponse cite une source privée de l’espace initial | Déplacement refusé si indépendance non démontrée, même sans evidence_id actif ; nouvelle conversation et transfert sélectif explicite proposés | C17 |

## Portabilité à toutes les phases

| ID | Scénario et action | Résultat attendu | Contrat |
| :--- | :--- | :--- | :--- |
| T-P01 | Windows propre, hors ligne, sans Python/Node/compilateur et sans runtime web présumé | Édition autonome complète démarre ; prérequis non embarqués déclarés avant distribution | C19 |
| T-P02 | Fermer, déplacer Thot dans un chemin avec espaces/accents puis une autre lettre de lecteur | Conversation, imports, modèles et créations retrouvés sans configuration manuelle | C10, C19 |
| T-P03 | Copier sur clé puis autre machine compatible avec autre nom d’utilisateur | Même données, bonne détection hôte, pas accès à un ancien répertoire utilisateur | C19 |
| T-P04 | Dossier read-only, disque plein puis saturation pendant écriture | Refus/erreur explicites, pas fallback silencieux ailleurs ni artefact incomplet déclaré prêt | C11, C19 |
| T-P05 | Ouvrir deux instances visant la même racine | Une seule instance écrivante, refus/rejoindre expliqué | C19 |
| T-P06 | « Préparer le déplacement » pendant un job | Attente/annulation puis fermeture ressources confirmée avant message prêt | C09, C19 |
| T-P07 | Coupure simulée sur une copie de test puis restauration | Récupération cohérente ou diagnostic précis, jamais effacement opportuniste du journal | C11, C19 |
| T-P08 | Copie des plus gros modèles sur support aux limites de fichiers connues | Préflight détecte incompatibilités ; pas de copie annoncée réussie d’un fichier tronqué | C19 |
| T-P09 | Paquets Linux/macOS sur cibles minimales propres et contrôle d’exécution réel | Bibliothèques/architecture/sécurité qualifiées, limites affichées ; pas équivalence déduite de Windows | C19 |
| T-P10 | Mettre à jour, migrer puis tenter d’ouvrir avec ancienne version | Sauvegarde valide, données préservées, version incompatible refusée proprement | C19 |
| T-P11 | Inspecter écritures hors racine et exporter le dossier | Exceptions documentées, secrets exclus par défaut ; pas promesse mensongère « zéro trace » | C18, C19 |
| T-P12 | Rejouer T-P01 à T-P11 après ajout image/audio/web/espaces | Chaque runtime ajouté respecte la même portée de portabilité | C02, C19 |

## Accessibilité transversale

| ID | Scénario et action | Résultat attendu |
| :--- | :--- | :--- |
| T-U01 | Tout le parcours principal au clavier | Focus visible, ordre cohérent, aucun piège de focus |
| T-U02 | Lecteur d’écran : statut, outils, erreurs, fin de génération | Libellés utiles, annonces non répétées à chaque token |
| T-U03 | Zoom, contraste, fenêtres réduites et longues réponses | Contenu essentiel lisible, actions d’arrêt/export accessibles |
| T-U04 | Micro désactivé et couleur non perceptible | Toutes les fonctions restent utilisables par texte/clavier et états textuels |

## Indicateurs à recueillir

Pour chaque profil d’inférence, mesurer pic VRAM ou mémoire unifiée, RAM résidente, disque temporaire, durée de chargement, latence de premier token, débit et temps d’arrêt. Les seuils d’acceptation chiffrés de confort seront choisis sur les profils réels, pas inventés dans cette documentation.

Pour la recherche, conserver un corpus de questions avec preuves attendues, taux d’appels réseau évitables, couverture des sous-questions, erreurs de citation et fuites de périmètre. Pour l’audio, consigner corpus français, erreurs de transcription, intelligibilité, silences/bruit et coût CPU.

Les critères de sécurité, absence de perte de données et isolation des contextes sont des portes de livraison. Une bonne vitesse ne compense pas un échec sur ces exigences.
