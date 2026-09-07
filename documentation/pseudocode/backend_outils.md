# Pseudo-code — Registre, lecture et vision

Cible conceptuelle non exécutée et non testée. Le registre et l’exécuteur appliquent les [contrats C02–C05, C07, C09–C13 et C18](../contrats_transversaux.md), y compris les droits dès la phase 1.

## Responsabilités et interfaces

- **QUI APPELLE** : l’orchestrateur propose un appel ; la boucle le confie à l’exécuteur déterministe ; les services locaux peuvent réutiliser les mêmes opérations de lecture autorisée.
- **POURQUOI** : borner une demande LLM avant d’ouvrir une ressource et transformer toute observation en résultat localisable.
- **NE PAS CONFONDRE** : catalogue et preuve, extraction textuelle et OCR, vision généraliste et génération d’image, `read_text` et lecture vocale.
- **Entrées** : nom d’outil actif, paramètres structurés, `artifact_id`, sélecteurs bornés, question, portée et budget de run. Aucun chemin proposé par le LLM.
- **Sorties** : résultat corrélé à `tool_call_id`, succès ou erreur, aperçus bornés, `evidence_id`, `artifact_id` éventuels, localisation, transformations et limites.
- **Phases** : [Phase 1] `get_source`, `read_text`, `vision`, `doc_ocr` ; [Phase 2] `generate_image` ; [Phase 3] `generate_file` ; [Phase 4] recherche autorisée ; [Phase 5] périmètre enrichi, sans nouvel accès global implicite.

## Registre et validation commune

```text
[Phase 1]
CONTRAT C02, C04, C09, C10, C18
QUI APPELLE : démarrage du backend, puis chaque proposition d’outil.
POURQUOI : l’absence d’un bouton UI n’est pas une permission backend.

AU DÉMARRAGE
    Enregistrer pour chaque outil un nom unique, une version de schéma,
    la phase d’activation, les capacités, les limites et le service responsable.
    Refuser les collisions de noms et les définitions incompatibles.
    N’exposer à l’orchestrateur que les outils actifs et autorisés pour le run.
    Ne pas exposer micro, TTS, suppression, shell ou accès SQL comme outils.

POUR CHAQUE PROPOSITION
    Vérifier nom, phase, version, types et champs inconnus.
    Vérifier bornes de tailles, pages, plages, question et coût prévu.
    Vérifier systématiquement l’acteur et la portée de lecture ou de production.
    POUR CHAQUE référence présente OU exigée par le schéma de cet outil
        Exiger la référence obligatoire si elle manque, sans en fabriquer une.
        Résoudre artifact_id ou evidence_id dans les droits du run courant.
        Pour une référence de fichier, vérifier ArtifactLink, disponibilité,
        MIME réel, état du Blob et chemin canonique sous la racine autorisée.
        Contrôler liens symboliques et jonctions, pas un préfixe texte.
    Un catalogue sans référence et une génération sans entrée n’exigent pas
    d’artifact_id artificiel ; leurs portées restent toujours vérifiées.
    SI ambiguïté de référence, droits insuffisants ou ressource absente
        Refuser avec motif borné ; ne pas tenter un autre fichier en silence.
    SI un hash est fourni à la place d’une autorisation
        Refuser ; ne pas révéler l’existence d’une ressource hors périmètre.
    SI schéma ou budget invalide
        Retourner un refus de proposition, sans effet disque, réseau ou modèle.
    SINON accepter l’appel et lui attribuer tool_call_id / logical_action_id.
```

## Flux principal d’exécution

```text
[Phase 1] [Phase 2] [Phase 3] [Phase 4] [Phase 5]
CONTRAT C04, C05, C07, C09, C11
QUI APPELLE : boucle, après validation ; aucun worker n’écrit sur stdout.

POUR UN APPEL ACCEPTÉ
    Persister la requête structurée puis émettre tool.started.
    Vérifier la clé idempotente et les effets déjà enregistrés.
    SI un résultat final existe, revalider ses droits puis le réutiliser sans répéter l’action.
    SINON
        Vérifier annulation, budget restant et droits immédiatement avant accès.
        Réutiliser l’admission et les budgets du run parent ; aucun nouveau slot lourd.
        Confier l’opération au service approprié.
        Pour une inférence, demander sa propre location via le socle.
        Ne jamais recevoir un handle LLM détenu par l’orchestrateur.
        Persister les gros corps comme extraction ou artefact interne.
        Produire des preuves avec extrait, source, localisation et version.
        Publier les fichiers selon la procédure de stockage, hors transaction longue.
    À LA FIN OU À L’ERREUR
        Attendre la fin native réelle si un worker est engagé.
        Enregistrer un seul résultat final corrélé, même si l’outil échoue.
        Retourner statut, aperçu, evidence_ids, artifact_ids et limites.
        Ne pas transporter le document entier dans tool.completed.
        La boucle ajoute requête et résultat avant toute nouvelle décision.
```

## Catalogue et sélection sans deviner

```text
[Phase 1]
CONTRAT C10, C12
QUI APPELLE : get_source ou préparation du catalogue compact.

LISTER LES RESSOURCES
    Filtrer d’abord les liens accessibles et la disponibilité.
    Présenter ID, nom, type détecté, provenance et ordre de dépôt.
    Conserver un ordre stable ; rendre la suite par pagination bornée.
    Ne pas charger les octets pour fabriquer une liste.
    Ne pas prétendre qu’une ressource répond à la question depuis son nom.

SI « cette image » désigne plusieurs artefacts non distingués
    Retourner un besoin de clarification avec candidats autorisés.
    Ne pas choisir le dernier dépôt arbitrairement.

[Phase 4]
    Ajouter les fiches sémantiques versionnées et leur qualité d’extraction.
    Les fiches aident au routage, jamais à certifier un fait non lu.
[Phase 5]
    Ajouter seulement les ressources explicitement partagées de l’espace courant.
```

## Lecture texte et PDF avant OCR

```text
[Phase 1]
CONTRAT C05, C07, C10, C12, C18
QUI APPELLE : read_text ou service d’extraction choisi depuis une demande bornée.
POURQUOI : ne pas transformer tout PDF textuel en lot d’images coûteux.

POUR UNE RESSOURCE TEXTE OU MARKDOWN
    Lire la copie gérée selon plage bornée ; vérifier encodage et taille.
    Conserver offsets ou numéros de lignes et version de l’extracteur.
    Retourner extrait, preuve et sélecteur de continuation si nécessaire.
    SI encodage non exploitable, annoncer l’échec sans inventer du texte.

POUR UN PDF
    Ouvrir ses métadonnées et vérifier nombre de pages et sélecteur demandé.
    SI fichier chiffré, corrompu ou plage invalide
        Retourner une erreur actionnable sans rendre tout le document.
    Extraire d’abord le texte exploitable des seules pages nécessaires.
    Évaluer couverture et qualité d’extraction, pas seulement présence de caractères.
    SI le texte suffit à la question
        Retourner preuves avec numéros de page, sans charger un modèle OCR.
    SINON
        Choisir les pages manquantes ou les régions utiles.
        Un besoin de tableau ou de mise en page peut justifier un rendu
        même lorsqu’une couche textuelle existe.
        Préparer un lot de pages dans les bornes pixels, mémoire et durée.
        Rendre seulement ce lot, avec paramètres du profil mesuré.
        Exécuter doc_ocr ou vision selon l’objectif.
        Libérer les rendus et buffers du lot avant de préparer le suivant.
        Vérifier annulation et budget entre les lots.
    Conserver ordre des pages, rotations, recadrages et autres transformations.
    SI budget atteint avant la fin
        Retourner un résultat partiel et les pages non traitées.
        Ne pas conclure que ces pages sont vides.
```

## Vision et OCR sous location contrôlée

```text
[Phase 1]
CONTRAT C04, C07, C12
QUI APPELLE : vision pour compréhension d’image, doc_ocr pour lecture visuelle.
NE PAS CONFONDRE : format bien formé et observation fiable.

POUR UNE IMAGE OU UN LOT DE PAGES AUTORISÉ
    Vérifier image décodable, dimensions, orientation et budget de pixels.
    Prétraiter la copie, sans modifier l’original géré ni externe.
    Tracer réduction, rotation ou recadrage ; ne pas imposer un réglage universel.
    Acquérir la location de famille vision/OCR sans location texte active.
    Vérifier modèle, projecteur et adaptateur d’architecture compatibles.
    SI un composant manque, refuser avec erreur spécifique ; pas de substitution.
    Préparer l’entrée multimodale dans l’adaptateur natif, hors IPC de contrôle.
    Poser la question bornée et recevoir l’observation.
    Signaler incertitude, texte illisible et erreurs possibles d’OCR.
    Enregistrer les preuves localisées et la provenance du traitement.
    Confirmer fin native puis libérer la location.
    Ne pas renvoyer le handle au demandeur.
    La boucle réacquiert le runtime texte avant de continuer.
```

## Erreurs et arrêts attendus

- [Phase 1] Outil inconnu, phase désactivée ou arguments hors schéma : aucun effet ; refus borné pris en compte dans le budget de décision. [C02, C04, C18](../contrats_transversaux.md)
- [Phase 1] Ressource inaccessible, ambiguë, absente, chiffrée ou corrompue : erreur distincte et possibilité de préciser/importer une autre ressource, jamais lecture hors portée. [C10, C12](../contrats_transversaux.md)
- [Phase 1] Annulation ou dépassement de budget : résultats déjà acquis conservés, arrêt coopératif puis worker isolé si nécessaire ; aucun faux timeout par thread. [C04, C07, C09](../contrats_transversaux.md)
- [Phase 4] Une fiche vide ou une extraction ratée ne prouve pas l’absence de réponse ; proposer ouverture ciblée ou autre stratégie locale dans les budgets. [C12, C15](../contrats_transversaux.md)

## Tests d’acceptation à rattacher

Scénarios associés à **T-C05, T-C06, T-C08, T-C12, T-F01 à T-F06, T-W05 et T-M01** dans la [matrice de tests](../tests_acceptation.md), à exécuter.

- [Phase 1] `artifact_id` d’une autre conversation et hash connu sans lien : même refus sans fuite d’existence.
- [Phase 1] Chemin relatif malveillant, lien symbolique sortant, MIME trompeur et image trop grande : aucun accès hors racine.
- [Phase 1] PDF textuel : texte extrait avant tout OCR ; PDF mixte : seules les pages nécessaires sont rendues.
- [Phase 1] PDF volumineux : mémoire bornée par lot, arrêt entre pages et résultat partiel localisable.
- [Phase 1] Deux images ambiguës : clarification ; fichier absent : erreur, pas sélection de secours.
- [Phase 1] Projecteur absent, échec natif et annulation : un résultat corrélé par appel accepté, ressources nettoyées avant reprise texte.
- [Phase 4] Catalogue sémantique non encore produit : catalogue minimal utilisable sans affirmation infondée.
- [Phase 1] Catalogue sans artifact_id et [Phase 2] génération sans référence : validation de portée maintenue sans identifiant d’entrée artificiel ; couverture de production à vérifier avec **T-I02**.

## Navigation

[Sommaire](README.md) · [Boucle](backend_agent_loop.md) · [Socle](backend_socle.md) · [Stockage](backend_db.md) · [Recherche](backend_recherche.md) · [Phase 1](../phase_1_socle_et_vision.md). Appui technique : [annexe LFM et architecture](../annexes/01_lfm_et_architecture.md).
