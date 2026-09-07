# Annexe 07 — Agents, recherche, mémoire et sécurité

Sources récupérées le **2026-09-06**. Conventions **Fait / Décision Thot / À mesurer** : voir le [sommaire des annexes](README.md).

La révision précédente de cette fiche citait le rapport technique du modèle texte comme référence agentique. Ce renvoi est retiré : les fondements ci-dessous sont les publications réellement pertinentes pour la récupération, la boucle d'outils et la mémoire.

## Génération augmentée par récupération (RAG)

**Fait.** « Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks » (Lewis, Perez, Piktus, Petroni, Karpukhin, Goyal, Küttler, Lewis, Yih, Rocktäschel, Riedel, Kiela) a été soumis le **22 mai 2020** et publié à **NeurIPS 2020** ([arXiv:2005.11401](https://arxiv.org/abs/2005.11401)).

**Fait.** Le modèle combine une **mémoire paramétrique** (un modèle séquence-à-séquence pré-entraîné) et une **mémoire non paramétrique** (un index vectoriel dense de Wikipédia interrogé par un récupérateur neuronal pré-entraîné) ; deux formulations sont comparées, l'une conditionnant toute la séquence sur les mêmes passages, l'autre autorisant des passages différents par jeton. Les motivations citées incluent la **provenance** et la **mise à jour des connaissances** ([arXiv:2005.11401](https://arxiv.org/abs/2005.11401)).

**Décision Thot.** La cascade de recherche de [C15](../contrats_transversaux.md) est une **politique produit**, pas une reproduction de cette architecture : Thot ne conditionne pas la génération sur un index global, il sélectionne des preuves localisées et autorisées par portée. La provenance, elle, est reprise comme exigence ([C16](../contrats_transversaux.md)).

## Boucle raisonnement–action (ReAct)

**Fait.** « ReAct: Synergizing Reasoning and Acting in Language Models » (Yao, Zhao, Yu, Du, Shafran, Narasimhan, Cao), première version le **6 octobre 2022**, version ICLR en mars 2023, fait **alterner traces de raisonnement et actions spécifiques à la tâche** : le raisonnement induit, suit et met à jour des plans et gère les exceptions, tandis que les actions interrogent des sources externes ([arXiv:2210.03629](https://arxiv.org/abs/2210.03629)).

**Décision Thot.** Thot retient l'alternance décision/observation, mais **pas** l'exposition des traces : conformément à [C03](../contrats_transversaux.md) et [C05](../contrats_transversaux.md), le raisonnement privé n'est ni stocké dans l'historique public ni transmis au générateur, et l'interface affiche des descriptions d'action, pas un monologue. Le point d'écart avec la publication est assumé et documenté.

## Mémoire d'agent et fonction de pertinence

**Fait.** L'architecture décrite par « Generative Agents » enregistre l'ensemble des expériences en langage naturel, **synthétise des réflexions de plus haut niveau** et **récupère dynamiquement** des souvenirs pour planifier ; une ablation montre que observation, planification et réflexion contribuent chacune au comportement obtenu ([arXiv:2304.03442](https://arxiv.org/abs/2304.03442)).

**Fait.** Le score de récupération est la somme pondérée de trois composantes normalisées **min-max dans [0,1]** — **récence** (décroissance exponentielle de facteur **0,995** sur les heures écoulées depuis la **dernière récupération** du souvenir), **importance** (note attribuée par le modèle de **1 à 10**) et **pertinence** (**similarité cosinus** entre vecteurs d'embedding) — avec des **pondérations toutes égales à 1** dans l'implémentation décrite ; les souvenirs les mieux classés qui tiennent dans la fenêtre de contexte sont insérés dans l'invite ([arXiv:2304.03442, version HTML](https://arxiv.org/html/2304.03442v2)).

**Décision Thot.** Cette formule est une **inspiration, pas un paramétrage à copier** : le facteur 0,995 est calibré sur un temps de jeu simulé, et les pondérations unitaires n'ont pas été validées pour un usage documentaire. Thot conserve donc les règles de [C17](../contrats_transversaux.md) : filtrage par droits et pertinence **avant** classement, score de fréquence/récence en signal **secondaire**, incrément d'usage seulement lorsque la preuve a réellement été retenue, et statut explicite des fiches (`candidate`, `accepted`, `stale`, `rejected`). Un score élevé n'est pas une validation de vérité.

**À mesurer** : pondérations et demi-vie adaptées à un usage bureautique ; effet de la promotion validée par l'utilisateur sur la qualité des réponses ; taille de page utile au-delà de la valeur initiale.

## Sécurité : contenus externes comme données non fiables

**Fait.** L'injection d'invite est **directe** lorsque l'entrée de l'utilisateur altère le comportement du modèle, **indirecte** lorsque le modèle ingère un contenu externe (site, fichier) qui l'altère ; les entrées peuvent être imperceptibles pour un humain, et **RAG comme le réglage fin n'éliminent pas** la vulnérabilité ([OWASP GenAI — LLM01 Prompt Injection](https://genai.owasp.org/llmrisk/llm01-prompt-injection/)).

**Fait.** Lorsque des outils ou systèmes connectés sont en jeu, les conséquences documentées incluent l'accès non autorisé aux fonctions du modèle, l'exécution de commandes dans les systèmes connectés et la divulgation d'informations sensibles ; les mesures recommandées comprennent **des jetons d'API propres à l'application**, le **traitement des fonctions extensibles dans le code** plutôt que par le modèle, la **restriction des privilèges au minimum** et une **validation humaine pour les opérations privilégiées**. Un scénario cité décrit une page contenant des instructions cachées qui provoque l'insertion d'une image pointant vers une URL, entraînant l'exfiltration de la conversation ([OWASP GenAI — LLM01](https://genai.owasp.org/llmrisk/llm01-prompt-injection/)).

**Décision Thot.** [C16](../contrats_transversaux.md) et [C18](../contrats_transversaux.md) traduisent ces recommandations : une page récupérée est une **donnée**, jamais une consigne système ; l'exécuteur déterministe détient les droits, pas l'orchestrateur ; le rendu des liens exclut tout HTML actif ; les opérations sensibles demandent une action utilisateur.

## Fraîcheur du cache web

**Fait.** Selon la RFC 9111, la **durée de fraîcheur** est l'intervalle entre la génération d'une réponse et son expiration ; une réponse est fraîche tant que son **âge** — temps écoulé depuis sa génération ou sa dernière validation réussie auprès du serveur d'origine — n'excède pas cette durée ([RFC 9111 — HTTP Caching](https://www.rfc-editor.org/rfc/rfc9111.html)).

**Fait.** La durée de fraîcheur se calcule par ordre de priorité : directive de cache partagé, puis directive d'âge maximal, puis en-tête d'expiration moins la date de la réponse, et à défaut une **fraîcheur heuristique** ; en cas de directives contradictoires, la plus restrictive doit être honorée, et les caches sont invités à traiter comme périmée toute réponse dont l'information de fraîcheur est invalide ([RFC 9111](https://www.rfc-editor.org/rfc/rfc9111.html)).

**Décision Thot.** Le snapshot de cache de [C16](../contrats_transversaux.md) enregistre donc la **date de récupération**, la date de publication si connue, le statut HTTP et les validateurs disponibles — de quoi appliquer une fraîcheur explicite au sens de la RFC — et non une simple date de collecte. Une réutilisation sans validateur est un choix de politique locale, à afficher, et jamais présenté comme une garantie d'actualité.

**À mesurer** : durées de fraîcheur par type de fait (prix, actualité, documentation stable) ; taux de revalidation utile ; coût du stockage des snapshots sur support portable.
