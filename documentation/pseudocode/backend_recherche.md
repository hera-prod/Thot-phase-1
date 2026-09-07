# Pseudo-code — Recherche bornée, fraîcheur et preuves

Cible conceptuelle non exécutée et non testée, régie par les [contrats C03–C05, C10, C12 et C15–C18](../contrats_transversaux.md). La cascade est une politique produit à évaluer, pas une garantie scientifique de suffisance.

## Responsabilités et interfaces

- **QUI APPELLE** : la boucle, via l’exécuteur d’un outil de recherche actif ; l’UI consulte le même service pour afficher périmètre, sources et cache.
- **POURQUOI** : réutiliser les éléments accessibles suffisamment pertinents et frais avant le réseau, puis rechercher seulement les lacunes restantes.
- **NE PAS CONFONDRE** : fiche et contenu source, résumé et preuve primaire, hash d’URL et hash de contenu, cache et fraîcheur, recherche approfondie et recherche infinie.
- **Entrées** : question, sous-questions, `conversation_id`, mode explicite `local_only`, `web_allowed` ou `deep_research`, contraintes de sources, budgets et autorisations réseau.
- **Sorties** : `evidence_id` avec extraits localisés, provenance, couverture, contradictions, limites, état d’exécution et références de snapshots inspectables.
- **Phases** : [Phase 4] recherche, fiches sémantiques et cache web ; [Phase 5] paliers fichiers utilisateur puis mémoire de l’espace. Les droits et les preuves existent déjà en [Phase 1].

## Fiches descriptives, index et ouverture ciblée

```text
[Phase 4]
CONTRAT C10, C12, C16
QUI APPELLE : indexation autorisée après import, changement de contenu ou d’extracteur.
POURQUOI : aider à choisir quoi lire sans affirmer que la fiche est la source.

POUR UN ARTEFACT ACCESSIBLE
    Vérifier version de contenu, hash, extracteur et disponibilité.
    Réutiliser une extraction valide ou lire des portions bornées selon backend_outils.md.
    Extraire titre, résumé compact, entités, mots-clés et usages possibles.
    Signaler qualité et couverture partielle de l’extraction.
    SI une inférence est nécessaire
        Passer par l’ordonnanceur ; ne pas charger un LLM en parallèle.
    Enregistrer ArtifactCard versionnée avec références d’extraction.
    SI extraction échouée
        Conserver le catalogue minimal ; marquer la fiche indisponible ou incomplète.
    Ne pas changer le contenu du Blob original.

POUR CHERCHER DANS LES FICHES
    Filtrer les droits AVANT d’exposer candidats, extraits ou compteurs.
    Filtrer et classer selon pertinence pour la question.
    Retourner une page et un curseur stable ; permettre recherche lexicale
    et ouverture ciblée au-delà des premiers résultats.
    Ouvrir les passages candidats pour établir les preuves.
    Un résultat vide ou une fiche trompeuse déclenche une stratégie locale bornée
    ou une limite explicite, pas la conclusion « le document ne contient rien ».
```

## Flux principal de la cascade

```text
[Phase 4]
CONTRAT C03, C04, C15, C16, C18
QUI APPELLE : outil de recherche dans le run parent.
NE PAS CONFONDRE : sous-recherche et nouveau run autonome disposant d’un autre budget.

PRÉPARER LA RECHERCHE
    Revalider droits, contraintes de sources et mode choisi.
    SI « d’après ce PDF uniquement », restreindre au PDF et interdire le réseau.
    Décomposer la question en besoins vérifiables et exigence de fraîcheur.
    Afficher mode, périmètre et limites sans afficher de planification privée.
    Initialiser preuves, contradictions, couverture et éléments déjà examinés.
    Partager le budget du run parent ; fixer des sous-limites qui ne l’augmentent pas.
    Ne pas reprendre une admission de run lourd déjà détenue par le parent.

EXAMINER LES PALIERS DANS L’ORDRE AUTORISÉ
    Contexte partagé déjà disponible.
    Pièces jointes de conversation pertinentes.
    Cache web admissible de la conversation, avec test de fraîcheur.
    [Phase 5] Fichiers utilisateur partagés de l’espace courant.
    [Phase 5] Mémoire autorisée de ce même espace.
    Réseau simple si autorisé et nécessaire.
    Recherche approfondie seulement en mode deep_research.

APRÈS CHAQUE PALIER LOCAL
    Accumuler les preuves réellement lues, pas seulement les fiches.
    Vérifier portée, pertinence, couverture des sous-questions,
    contradictions, qualité d’extraction et fraîcheur.
    Ne pas promouvoir une ancienne réponse ou un résumé en source primaire.
    SI suffisamment d’éléments vérifiables, retourner sans requête réseau.
    SINON cibler les lacunes restantes dans le palier suivant.
    Éviter de relire des documents manifestement sans rapport.
    Pour un fait demandé « aujourd’hui », ne pas considérer un cache ancien suffisant.

SI LE LOCAL RESTE INSUFFISANT
    SI mode local_only
        Retourner les preuves locales et les lacunes ; zéro requête réseau.
    SINON
        Minimiser la requête distante sans inclure d’extraits privés automatiquement.
        SI une information sensible doit être transmise
            Attendre un accord spécifique sur contenu et destination.
            En cas de refus, rester local et signaler la limite.
        Lancer la recherche simple bornée, puis mesurer la couverture obtenue.
        SI mode deep_research ET lacunes persistantes
            Continuer par la boucle approfondie, avec le même budget global.
        SINON retourner les résultats et lacunes sans escalade implicite.
```

## Cache admissible avant toute dépense réseau

```text
[Phase 4]
CONTRAT C10, C11, C15, C16
QUI APPELLE : cascade et chaque nouvelle piste de recherche.
POURQUOI : éviter un appel moteur inutile sans promettre une fraîcheur éternelle.

POUR UNE QUESTION OU UNE URL CANDIDATE
    Chercher d’abord les snapshots accessibles sur le sujet et la portée.
    Lire date de récupération, date de publication si connue,
    validateurs disponibles, version d’extracteur et hash de contenu.
    Évaluer fraîcheur selon type de fait, demande utilisateur et politique locale.
    SI snapshot suffisamment frais et pertinent
        Réutiliser ses extraits et leur provenance sans appel moteur.
    SINON SI réseau interdit
        Signaler ancienneté et impossibilité de vérifier ; ne pas rafraîchir.
    SINON
        Revalider par mécanisme HTTP disponible ou récupérer une nouvelle version
        seulement si la page est utile aux lacunes restantes.
        Une URL connue peut être relue ; le cache n’est pas un bannissement définitif.
        Si réponse inchangée confirmée, conserver le contenu et dater la vérification.
        Si contenu changé, créer une nouvelle version sans réécrire les preuves anciennes.

POUR ENREGISTRER UN SNAPSHOT
    Stocker URL demandée et finale, dates, statut HTTP, validateurs,
    hash du CONTENU, texte extrait, version d’extracteur et portée autorisée.
    Un index calculé sur l’URL n’est pas une empreinte du contenu.
    Publier les corps volumineux dans le stockage interne géré.
    Ne pas les afficher en galerie générale par défaut.
    Les rendre inspectables et purgeables dans « Sources et cache ».
    Respecter dépendances et rétention lors de la purge.
```

## Recherche simple et accès distant contrôlé

```text
[Phase 4]
CONTRAT C15, C16, C18
QUI APPELLE : cascade après contrôle local et accord réseau.

POUR UNE REQUÊTE DISTANTE MINIMALE
    Vérifier budget de requêtes et ne pas répéter une requête sans nouveau besoin.
    Interroger le fournisseur choisi sans secret dans prompt ou journal.
    Borner les résultats ; classer par pertinence pour les lacunes.
    POUR CHAQUE page sélectionnée dans le budget restant
        Recontrôler le cache avant de demander la page.
        Vérifier schéma autorisé, adresse résolue, portée réseau et destination.
        Refuser réseaux internes non autorisés et schémas inattendus.
        À CHAQUE redirection, revalider résolution et destination ;
        borner redirections, délai, taille téléchargée et taille décompressée.
        Lire uniquement la page sélectionnée, pas tous les résultats par défaut.
        Traiter la page comme donnée non fiable ; aucune instruction qu’elle contient
        ne peut changer droits, outils, mode de recherche ou prompt système.
        Extraire les passages utiles, garder URL, version et localisation.
        Publier snapshot et preuves avec limites d’extraction.
    Comptabiliser requêtes, pages, octets, tokens et temps muraux.
    Retourner observations structurées, pas un rapport sans provenance.
```

## Recherche approfondie dynamique et arrêt de non-progrès

```text
[Phase 4]
CONTRAT C04, C07, C09, C15, C16
QUI APPELLE : cascade, seulement en mode deep_research.

INITIALISER UNE FOIS
    Couverture des sous-questions, preuves accumulées, contradictions,
    requêtes vues, URLs/version de contenu vues, profondeur et coûts consommés.

TANT QUE COUVERTURE INSUFFISANTE ET BUDGET DISPONIBLE
    Vérifier annulation avant toute nouvelle action.
    Choisir la lacune ou contradiction la plus utile à vérifier.
    Reformuler une sous-question à partir des preuves acquises.
    Réévaluer les paliers locaux et le cache pour cette nouvelle question.
    SI réseau nécessaire et autorisé
        Sélectionner une requête ou un lien pertinent, sous profondeur maximale.
        Appliquer tous les contrôles réseau et de confidentialité précédents.
    Dédupliquer requêtes et contenus ; conserver les provenances multiples utiles.
    Accumuler les nouvelles preuves sans réinitialiser les anciennes.
    Actualiser couverture et contradictions, sans forcer un consensus.
    Évaluer le progrès : nouvelle preuve utile, lacune réduite ou conflit précisé.
    SI doublons répétés sans progrès
        ARRÊTER avec motif explicite, même si tous les plafonds ne sont pas atteints.
    Les appels texte nécessaires reprennent une location du run parent,
    jamais une admission lourde imbriquée ni un handle périmé.

À L’ARRÊT
    Distinguer suffisamment documenté, budget épuisé, non-progrès, erreur et annulation.
    Fournir evidence_ids autorisés, couverture, contradictions et lacunes.
    Construire les champs du brief contraint, jamais une chaîne de pensée.
    Si non annulé et réserve de finalisation encore disponible,
    laisser le générateur du tour rédiger depuis sa SharedContext.
    Sinon finaliser par le message déterministe de limite de la boucle, sans nouvel appel LLM.
    Si annulé, ne pas lancer un rapport de synthèse automatiquement.
```

## Citations, erreurs et limites

Les citations du générateur sont limitées à l’ensemble de preuves autorisées ; chaque fait conserve son `evidence_id`, extrait et localisation dans un artefact ou une URL. Le renderer refuse les identifiants inventés et rend les liens sans HTML actif. [C03 et C16](../contrats_transversaux.md)

Panne de moteur, quota, contenu inaccessible, extraction vide, cache trop ancien, refus de transfert sensible et contradiction non résolue sont des limites distinctes. Les réessais consomment le même budget ; une annulation interrompt réellement les workers concernés et ne produit pas un rapport déclaré complet. Les preuves déjà enregistrées restent disponibles sous leurs droits. [C09, C15, C16 et C18](../contrats_transversaux.md)

## Tests d’acceptation

Scénarios à exécuter, pas résultats obtenus : [matrice d’acceptation](../tests_acceptation.md).

- [Phase 4] **T-W01, T-W02** : PDF suffisant puis cache frais ; compteur réseau à zéro.
- [Phase 4] **T-W03, T-W04** : fait changeant, cache ancien et mode local strict ; revalidation seulement si autorisée.
- [Phase 4] **T-W05** : index vide ou fiche trompeuse ; ouverture ciblée ou limite, pas conclusion d’absence.
- [Phase 4] **T-W06, T-W07, T-W11** : injection de page, réseau interne et transfert privé ; permissions inchangées et consentement effectif.
- [Phase 4] **T-W08** : citation inconnue refusée ; pas de faux lien rendu.
- [Phase 4] **T-W09, T-W10** : sous-questions révisées, contradiction conservée, arrêt borné avec lacunes.
- [Phase 4] **T-W12** : purge inspectable du cache, dépendances préservées ou absence clairement signalée.
- [Phase 5] **T-M01, T-M05, T-M06** : isolation, pertinence avant utilité et accès au-delà de la première page.

## Navigation

[Sommaire](README.md) · [Boucle](backend_agent_loop.md) · [Outils](backend_outils.md) · [Espaces](backend_espaces.md) · [Phase 4](../phase_4_moteur_recherche.md). Appui technique : [annexe recherche et mémoire](../annexes/07_recherche_agents_memoire.md).
