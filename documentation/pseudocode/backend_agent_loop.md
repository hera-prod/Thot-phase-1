# Pseudo-code — Tour, outils et contexte partagé

Cible conceptuelle non exécutée et non testée, subordonnée aux [contrats C01–C09, C12 et C16](../contrats_transversaux.md). L’orchestrateur propose, l’exécuteur valide et agit, le générateur rédige sans outils.

## Responsabilités et interfaces

- **QUI APPELLE** : l’ordonnanceur prend un run accepté par `chat.send` ; la boucle appelle stockage, registre d’outils et runtime.
- **POURQUOI** : conserver les résultats utiles d’une décision à l’autre sans transmettre le raisonnement privé au générateur.
- **NE PAS CONFONDRE** : même modèle texte, même révision du contexte de base et prompts de rôles différents ; le générateur n’est pas l’exécuteur.
- **Entrées** : `conversation_id`, message utilisateur déjà persisté, `run_id`, phase active, droits, budgets et intention d’annulation.
- **Sorties** : message assistant public avec statut, journal technique borné, preuves localisables, artefacts autorisés, terminal unique après persistance.
- **Phases** : [Phase 1] boucle et compaction ; [Phase 2] outil image ; [Phase 3] outil fichier ; [Phase 4] recherche ; [Phase 5] sources d’espace. Les outils futurs restent absents avant leur phase.

## Préparation d’un tour et identité du contexte

```text
[Phase 1]
CONTRAT C01, C03, C05, C06, C09
QUI APPELLE : ordonnanceur, après acceptation durable de la commande.
POURQUOI : éviter un utilisateur en double ou deux historiques divergents.

AU DÉBUT DU RUN
    Prendre la sérialisation de contexte de la conversation.
    Utiliser un mutex applicatif, pas une transaction SQL gardée pendant le tour.
    Réutiliser l’admission lourde du run attribuée par l’ordonnanceur ;
    compacteur, outils et sous-recherches ne la réacquièrent pas.
    Vérifier message_id, droits courants et idempotence de la tentative.
    Vérifier que ce run est l’unique chat non terminal admis pour la conversation.
    Cette exclusivité a été garantie atomiquement AVANT insertion du message,
    pas seulement par le mutex acquis au démarrage de la boucle.
    Ne pas réinsérer le message utilisateur déjà enregistré à l’acceptation.
    Persister le passage queued à running, puis émettre run.started.
    Initialiser une seule fois le budget du run avant toute compaction.
    Réserver dans ce budget une enveloppe de finalisation en tokens ET durée,
    distincte de la seule réserve de sortie dans la fenêtre du modèle.
    Charger la révision courante et le snapshot actif éventuellement validé.
    Vérifier que sa borne précède le message utilisateur de ce tour.
    Lire les échanges publics ordonnés strictement après cette borne.
    Préparer les composants de la future base SharedContext :
        résumé actif et borne, suffixe public, message courant exactement une fois,
        catalogue compact des ressources autorisées.
    Vérifier qu’aucun message couvert n’est aussi présent dans le suffixe.
    Compter les prompts avec tokenizer et template effectifs de chaque rôle.
    Réserver dès maintenant les schémas/outils et un volume borné d’observations.
    SI une compaction préalable est nécessaire
        Appliquer le flux de compaction ci-dessous AVANT de figer la base.
        Comptabiliser ses coûts dans le budget déjà ouvert.
        Relire snapshot actif et suffixe à sa nouvelle borne, sans perdre le message courant.
    Construire une seule base SharedContext(conversation_id, revision) validée.
    Figer SharedContext et son identité pour toute cette tentative.
    Initialiser UNE FOIS, hors de la boucle :
        séquence structurée des appels et résultats du tour,
        preuves accumulées, artefacts accumulés, limites et erreurs,
        empreintes d’actions et état de progrès.
    Conserver les compteurs déjà consommés par la préparation ; ne pas les remettre à zéro.
    Ne pas émettre de raisonnement.
```

## Flux principal de décision et d’exécution

```text
[Phase 1] [Phase 2] [Phase 3] [Phase 4] [Phase 5]
CONTRAT C03, C04, C05, C07, C09
QUI APPELLE : boucle de tour ; les outils passent par backend_outils.md.
NE PAS CONFONDRE : journal complet borné et sélection utile à l’inférence.

TANT QUE LE RUN N’EST PAS ARRÊTÉ
    SI annulation demandée
        Sortir vers le flux d’annulation, sans nouvelle génération.
    SI limite de décisions, appels, tokens, volume ou durée atteinte
        Noter la limite ; sortir vers le contrôle de finalisation ci-dessous.
    Ne pas dépenser dans une décision ou un outil l’enveloppe réservée à la finalisation.
    Vérifier que le prompt de décision tient, réserves et marge comprises.
    SI impossible sans changer la base figée
        Ne pas tronquer en silence ni construire une autre base pour un seul rôle.
        Arrêter avec limite explicite ; proposer une nouvelle tentative compactée.
    Acquérir une location texte non imbriquée.
    Reconstituer l’entrée depuis la MÊME SharedContext figée, le système
    orchestrateur, les outils autorisés, les appels/résultats accumulés
    et le budget restant.
    Demander une décision structurée sans demander de chaîne de pensée.
    Décoder les canaux et appels selon le template effectif.
    Libérer la location texte et invalider son jeton avant tout outil.
    Comptabiliser les tokens et la décision.

    SI la sortie est un brief
        Valider le brief selon le flux ci-dessous, puis arrêter les décisions.
    SINON SI aucun appel ni brief exploitable
        Noter une sortie invalide ; reconstruire un brief minimal.
        Arrêter les décisions plutôt que transmettre le texte interne.
    SINON
        POUR CHAQUE appel proposé, dans l’ordre retenu
            Valider schéma, phase, portée et bornes par l’exécuteur.
            SI proposition invalide
                Ajouter un refus structuré borné au tour, sans effet système.
                Comptabiliser l’échec ; ne pas permettre une boucle illimitée.
            SINON
                Attribuer tool_call_id et logical_action_id stables.
                Ajouter la requête VALIDÉE à la séquence et au journal.
                SI même action idempotente déjà terminée
                    Revalider ses droits puis relire son résultat ; ne pas répéter ses effets.
                SINON SI annulation, erreur fatale ou budget atteint avant démarrage
                    Produire son résultat corrélé cancelled ou limite.
                SINON SI doublon sémantique sans nouveau sélecteur ni progrès
                    Produire son résultat corrélé de non-progrès.
                    Noter le motif d’arrêt ; ne pas relancer l’outil.
                SINON
                    Exécuter via service déterministe et ordonnanceur.
                    Attendre fin confirmée, y compris après escalade d’annulation.
                    Convertir aussi les échecs en résultat structuré.
                Enregistrer EXACTEMENT UN résultat par tool_call_id.
                Ajouter ce résultat à la séquence AVANT la prochaine décision.
                Accumuler les nouvelles preuves et références d’artefacts
                sans effacer celles des décisions précédentes.
                Stocker les gros corps hors des messages ; garder aperçu et ID.
                Émettre tool.completed après enregistrement.
                Décompter appels, temps, taille et tokens réellement utilisés.
        SI vision ou image a déchargé le texte
            Oublier tout ancien handle ; la prochaine acquisition sera neuve.
        SI erreur fatale, annulation ou non-progrès confirmé
            Arrêter la boucle selon le motif, pas par un succès implicite.
```

Les cinq décisions du chat simple sont un réglage initial modifiable, non une limite universelle. Les durées d’OCR, de diffusion et de recherche ont des budgets adaptés ; aucun timeout d’attente ne vaut preuve d’arrêt natif. [C04, C07 et C09](../contrats_transversaux.md)

## Brief contraint et génération publique

```text
[Phase 1]
CONTRAT C03, C04, C05, C06, C16
QUI APPELLE : boucle sortie normalement ou avec résultats partiels utilisables.
POURQUOI : transmettre un objectif et des observations, pas un monologue interne.

CONTRÔLER LA FINALISATION
    SI annulation, aller directement au terminal d’annulation sans appel LLM.
    SI les tokens OU la durée nécessaires ne restent pas dans le budget total
        Construire un message de limite déterministe, sans appel LLM supplémentaire.
        Mentionner les seules observations et sorties déjà validées et accessibles.
        Persister ce message partial et clôturer le run avec sa limite explicite.
        Ne pas lancer compacteur, résumé auxiliaire ou générateur pour « finir quand même ».
    SINON utiliser l’enveloppe de finalisation encore disponible,
    toujours comprise dans le budget du run ; vérifier aussi la fenêtre de contexte.

VALIDER LE BRIEF
    Autoriser seulement user_goal, response_constraints, evidence_ids,
    artifact_ids, unresolved_points et execution_status.
    Refuser champs thinking, reasoning et tout champ supplémentaire.
    Vérifier types, taille, provenance et absence de narration de planification.
    Comparer contraintes et objectif à la demande utilisateur originale.
    Résoudre chaque identifiant, vérifier la portée et l’existence de son contenu.
    SI texte libre, champ interdit, instruction contradictoire ou ID inventé
        Rejeter le brief proposé ; reconstruire un brief minimal déterministe
        depuis demande, résultats validés et limites réellement rencontrées.
    Ne jamais copier le transcript de l’orchestrateur pour réparer un brief.

PRÉPARER LE GÉNÉRATEUR
    Revalider les droits ; si la portée a changé, arrêter cette tentative
    plutôt que modifier silencieusement une seule copie de SharedContext.
    Sélectionner des observations localisées dans le budget disponible.
    Conserver références, chiffres critiques et incertitudes.
    SI corps trop gros
        Lire des passages bornés ou produire une synthèse traçable ;
        signaler ce qui n’a pas été inclus, sans troncature invisible.
        Tout appel auxiliaire doit tenir dans le budget restant et utiliser
        une location du run parent ; sinon sélectionner des extraits sans modèle.
    Compter système générateur, MÊME SharedContext(conversation_id, revision),
    brief, observations, références, réserve de sortie et marge.
    SI ce prompt ne tient pas, réduire les ajouts de façon explicite ;
    SI la base seule ne tient pas, arrêter et proposer une reprise compactée.
    Recontrôler l’enveloppe de finalisation après la préparation des observations.
    SI elle ne permet plus le chargement et la génération dans tokens et durée restants,
    produire la limite déterministe, sans lancer le runtime pour une dernière réponse.
    Réacquérir le runtime du même modèle texte par l’ordonnanceur.
    Ne fournir ni outils, ni transcript brut, ni raisonnement privé.
    Produire les seuls canaux publics décodés par le template.
    Surveiller aussi durée et tokens restants pendant la génération ;
    arrêter nativement au budget, conserver le texte partial et finaliser sans autre appel LLM.
    Valider les références sur l’ensemble autorisé avant leur rendu.
    Émettre answer.delta bornés ; ne pas afficher une citation non résolue.
    Enregistrer la réponse publique et son statut avant le terminal du run.
```

## Compaction non destructive

```text
[Phase 1]
CONTRAT C03, C05, C06
QUI APPELLE : préparation sérialisée d’un tour, avant gel de SharedContext.
NE PAS CONFONDRE : projection d’inférence et messages originaux.

SI LE PROMPT DU PROCHAIN RÔLE APPROCHE OU DÉPASSE LE BUDGET RÉEL
    Traiter le seuil autour de 110 000 tokens comme signal configurable.
    Réserver système, template, outils, observations, sortie et marge.
    Choisir une borne d’historique laissant le message courant hors du résumé.
    Lire le préfixe public et le résumé précédent avec références durables.
    SI l’entrée du compacteur est trop longue
        Résumer des segments bornés, puis synthétiser dans une fenêtre mesurée.
    Utiliser le compacteur sans outils et sans raisonnement privé.
    Construire un ContextSnapshot candidat avec révision de départ,
    covered_through_message_id, résumé, références et incertitudes.
    Valider taille, borne, cohérence des références et absence de doublons.
    En transaction courte, comparer la révision de conversation.
    SI validation réussie ET révision inchangée
        Activer atomiquement ce snapshot ; garder les précédents selon rétention.
        Conserver TOUS les messages originaux et leur accès ciblé.
    SINON
        Ne pas activer le candidat ; conserver ancien snapshot et historique.
        Signaler l’échec ou le conflit ; reprise bornée ou profil réduit explicite.
    Ne pas compacter sous les pieds d’un run utilisant une base déjà figée.
```

## Erreurs, arrêt et persistance

```text
[Phase 1]
CONTRAT C04, C05, C08, C09

SI arrêt normal
    Persister la réponse complete et le run completed ; publier run.completed.
SI budget épuisé ou outil récupérable en échec
    Appliquer le contrôle de finalisation ; sans réserve, message déterministe
    de limite, sans nouvel appel LLM ; sinon génération bornée et non annulée.
    Persister la réponse partial et le run completed ;
    publier run.completed avec statut de réponse partiel.
SI annulation
    Persister le passage à cancelling tant que l’arrêt natif est en cours.
    Arrêter le worker selon le socle ; conserver les sorties déjà publiées.
    Persister la réponse interrompue cancelled, même si elle contient du texte.
    Publier run.cancelled ; ne pas lancer de nouvelle réponse.
SI erreur fatale
    Persister failed et les informations publiques disponibles.
    Publier run.failed ; ne pas maquiller l’échec en réponse complète.
SI crash ou commit impossible
    Ne pas annoncer un terminal durable fictif.
    Laisser la récupération marquer interrupted et réconcilier les effets.
    À la resynchronisation, signaler run.failed avec code RUN_INTERRUPTED.

TOUJOURS
    Journaliser arguments validés, résultats/provenances, erreurs et durées.
    Ne pas enregistrer le raisonnement brut ni le placer dans Message.content.
    Ne pas réinjecter intégralement les journaux des tours anciens.
    Libérer la sérialisation de conversation après sauvegarde ou récupération.
```

## Tests d’acceptation à rattacher

Scénarios associés à **T-C01 à T-C12, T-K01 à T-K05, T-R02 à T-R06 et T-W08** dans la [matrice de tests](../tests_acceptation.md), sans les déclarer exécutés.

- [Phase 1] Deux décisions avec outils puis un brief : tous les appels et résultats sont présents, chaque résultat est unique, les preuves du premier appel subsistent.
- [Phase 1] Après vision, le générateur retrouve exactement la révision de base de l’orchestrateur et le message courant une seule fois, avec un nouveau handle texte.
- [Phase 1] Brief libre, `reasoning`, ID inventé et contrainte contradictoire : réparation minimale sans raisonnement ni perte de la demande.
- [Phase 1] Vérifier séparément historique public, journal durable et absence de raisonnement ; du français légitime entre crochets reste intact.
- [Phase 1] Compaction réussie, trop longue, échouée et concurrente à un changement de révision : originaux conservés, aucun trou ni double couverture.
- [Phase 1] Annulation native, budget épuisé, doublon sans progrès et crash après publication : états distincts et aucun effet dupliqué.
- [Phase 4] Citation inventée et source devenue inaccessible : pas de lien rendu comme valide.
- [Phase 1] **T-R10, T-R11, T-R13** : une admission parent, une seule demande active par conversation, aucune génération supplémentaire sans réserve de finalisation.

## Navigation

[Sommaire](README.md) · [Socle](backend_socle.md) · [Outils](backend_outils.md) · [Stockage](backend_db.md) · [Phase 1](../phase_1_socle_et_vision.md). Appui technique : [annexe LFM et architecture](../annexes/01_lfm_et_architecture.md).
