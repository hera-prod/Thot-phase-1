# Audit de cohérence de la préproduction Thot

Cette révision du 6 septembre 2026 corrige la documentation et le pseudo-code, sans modifier le code applicatif. Le point de comparaison est le commit `9646e60d526c7c28705df2dbef2722966f6644ef` ; les constats sur le prototype proviennent d’une lecture statique, pas d’essais d’exécution.

## Conclusion et limites

La première documentation n’était pas suffisamment cohérente pour servir de contrat d’implémentation. La révision remplace ses règles contradictoires par les [contrats transversaux C01 à C19](contrats_transversaux.md), des phases alignées et du pseudo-code conceptuel annoté ; elle distingue exigences utilisateur, recommandations et questions à qualifier.

La portabilité par dossier devient une exigence de socle, non une finition de phase 5. Elle reste à démontrer par construction et essais de paquets sur machines propres ; ni la compatibilité Windows/Linux/macOS, ni le grand contexte sous 8 Go, ni les performances de génération ne sont certifiés par cette révision.

## Méthode et périmètre

- **Lecture de référence** : README et documentation du commit initial, avec confrontation ciblée aux chemins actifs du prototype.
- **Relecture indépendante** : 31 constats sur cette version, classés en 7 priorités de conception P0, 21 P1 et 3 P2. Ces niveaux désignent l’importance avant implémentation, pas des incidents observés en production.
- **Vérification technique** : sources primaires rassemblées et délimitées dans les [annexes](annexes/README.md). Un papier, une documentation amont et un résultat matériel utilisateur n’apportent pas la même preuve.
- **Révision contractuelle** : définition des identités, responsabilités, frontières de confiance, budgets, échecs et tests attendus ; réécriture des cinq phases et des neuf modules de pseudo-code.
- **Contrôle documentaire** : liens locaux, ancres, structure Markdown et périmètre Git. Les résultats définitifs de ce contrôle sont consignés plus bas après la dernière relecture.

L’absence des phases futures dans le prototype n’est pas une anomalie en soi. En revanche, une promesse présentée comme déjà implémentée ou un contrat qui contredit un autre document doit être corrigé.

## Corrections des contradictions initiales

Les références Axx correspondent au relevé de lecture de la version initiale. Le tableau indique la correction de conception retenue ; il ne signifie pas que le prototype a été corrigé.

| Constats | Problème dans la documentation précédente | Règle de remplacement et traçabilité |
| :--- | :--- | :--- |
| A01 | Générateur privé du dialogue commun et brief libre pouvant contenir la planification | Même `SharedContext.revision`, systèmes distincts, brief fermé, aucune chaîne de pensée ; C03, T-C03/T-C04/T-C07 |
| A02 | Résultats remis à zéro, absence de retour explicite à la limite, appels/résultats incomplets | Journal accumulé, résultat corrélé pour chaque appel valide, arrêt borné ; C04, T-C05/T-C09/T-C12 |
| A03–A04 | Permissions reportées, chemins libres, documents promus en instructions | Refus par défaut, portée vérifiée côté backend et sources non fiables ; C10/C18, T-F02/T-W06 |
| A05 | Seuil 110k supposé universel, budgets par résultat sans budget de prompt complet | Tokenizer/template réels, réserve sortie, base figée après compaction, observations bornées ; C06, T-K01 à T-K05 |
| A06 | Annuler l’attente assimilé à arrêter l’inférence | Coordinateur disponible, worker isolable, arrêt confirmé avant libération ; C07/C09, T-R02/T-R05 |
| A07, A16–A17 | Chemins absolus, Python hôte, absence de règles USB et paquet autonome | Racine portable, imports gérés, paquets par cible, fermeture/sauvegarde ; C19, [portabilité](portabilite.md), T-P01 à T-P12 |
| A08–A09 | Raisonnement stocké et interdit selon le fichier ; compaction pouvant être interprétée comme suppression | Dialogue, journal et contexte distincts ; snapshots non destructifs et versionnés ; C05/C06, T-C10/T-K02 |
| A10–A11 | « Un modèle » supposé borner la VRAM ; handles et nettoyage implicites | Location par famille de pipeline, composants complets, états/générations, admission de run distincte ; C07, T-R01/T-R03/T-R10 |
| A12–A15, A30 | Enveloppes incompatibles, stream surdécrit comme acquis, terminal avant persistance, disponibilité/abonnements flous | IPC versionné, état reconsultable, corrélation, commit avant terminal logique, reprise après coupure ; C08/C09, T-R04/T-R07/T-R09 |
| A18–A20 | `Session`/`Conversation`, `Source`/`Artifact`, galerie et migrations incohérents | Noyau unique, blobs et liens multi-portées, activation progressive, studio sans faux chat ; C01/C02/C10/C13, T-F07/T-I08 |
| A19 | Hash utilisé comme intégrité présumée ou droit, suppression dangereuse | Hash du contenu vérifié, autorisation séparée, publication en deux étapes et collecte contrôlée ; C10/C11, T-R06/T-F07 |
| A21–A22 | Deux contrats d’outils ; retrait du projecteur ou changement de handler comme repli | Chemin validé unique, IDs autorisés, erreurs explicites sans substitution d’architecture ; C04/C07/C12, T-C06/T-F06 |
| A23 | FLUX présenté avec composants et consommation supposés | Pipeline exact à identifier, métadonnées complètes et profil utilisateur à reproduire ; C13/C19, O03, T-I02/T-I06 |
| A24–A26 | Cache consulté trop tard ou jamais rafraîchi, proximité assimilée à suffisance, recherche non bornée | Fraîcheur/portée/couverture, modes réseau explicites et budget global ; C15/C16, T-W01 à T-W12 |
| A27 | Score d’utilité assimilé à vérité, cinq résultats devenant une barrière | Pertinence avant utilité, pagination, validation mémoire humaine initiale ; C17, T-M04 à T-M08 |
| A28–A29 | Audio dans JSON, conflits de brouillon, génération documentaire supposée sûre | Références contrôlées, révision du brouillon, quotas CPU et schémas de documents sans code arbitraire ; C13/C14, T-D01 à T-A06 |
| A31 | Navigation locale cassée | Liens relatifs corrigés et contrôle des ancres ; sommaire central, annexes et pseudo-code reliés |

Les assertions techniques non établies de l’ancienne annexe ne sont pas conservées pour paraître cohérentes avec l’historique. Les nouvelles annexes doivent notamment distinguer les propriétés réelles de LFM, les composants de FLUX, les variantes audio, leurs licences et les contraintes natives de distribution.

## Écarts statiques du prototype à traiter lors du codage

Les chemins ci-dessous désignent le code du commit de référence. Ils servent de points d’entrée à l’utilisateur qui implémentera les contrats ; aucun exploit, crash matériel ou test d’inférence n’a été réalisé ici.

| Zone inspectée | Observation sur le chemin actuel | Conséquence pour le développement |
| :--- | :--- | :--- |
| `backend/config/paths.py`, `backend/db/media_cache.py`, `src-tauri/src/lib.rs` | Racines liées au compte hôte, à CWD, à `/tmp/thot` et à un chemin développeur ; racines Rust/Python divergentes | Unifier la résolution de chemins et distinguer strict portable/installé avant packaging |
| `src-tauri/src/lib.rs` | Des commandes d’historique lancent `python3 -c` ; `chat_send` peut retourner succès sans sidecar | Supprimer la dépendance implicite à Python hôte dans le futur code et rejeter l’indisponibilité |
| `src-tauri/tauri.conf.json` | `csp` nul et liste de ressources vide dans la configuration inspectée | Revoir sécurité de rendu et contenu réel du paquet ; ces lignes seules ne démontrent pas un exploit ni la totalité de la configuration de build |
| `backend/inference/ipc.py`, `backend/main.py`, frontend chat | Handler de tour synchrone dans la lecture stdin ; annulation sans arrêt natif démontré | Rendre le contrôle indépendant, définir l’acquittement et les états avant d’afficher « arrêté » |
| `backend/inference/loop.py` | Le code transmet déjà le dialogue aux deux rôles et accumule les observations | Préserver ces comportements : les défauts A01/A02 concernaient surtout le pseudo-code, pas une perte de dialogue actuelle démontrée |
| `backend/inference/loop.py` | Certains accès se résolvent par ID global/chemin sans filtre de conversation systématique ; observations ajoutées au rôle système | Ajouter autorisation par portée et frontière de confiance, indépendamment de la bonne volonté du modèle |
| `backend/inference/loop.py`, `backend/tools/base.py` | Dispatch actif par fonctions parallèles au registre, sans passer par `BaseTool.call` et ses validations ; plusieurs appels annoncés pour un seul exécuté | Unifier l’adaptateur d’appels, la validation et les résultats corrélés |
| `backend/config/defaults.py`, `backend/inference/manager.py` | Contexte adapté à des heuristiques matérielles ; replis de handlers/projecteur dans le manager | Reproduire la configuration utilisateur avant de choisir le profil et éliminer les replis qui retirent la capacité demandée |
| `backend/inference/compaction.py`, `backend/db/repositories/conversations.py` | Résumeur présent mais non raccordé au chemin actif inspecté ; estimation approximative des tokens | Implémenter snapshot actif, budget réel et reprise ; aucune destruction de transcript n’a été démontrée |
| `backend/inference/loop.py`, `backend/main.py` | Signal de fin de réponse avant commit assistant dans le chemin inspecté | Distinguer fin du stream, persistance, terminal et reconnexion |
| `frontend/src/pages/chat/ChatConversation.tsx` | Abonnements asynchrones et réponses d’historique sans invalidation complète ; délai arbitraire avant premier envoi | Tester navigation rapide, démontage tardif, readiness et récupération ; courses possibles déduites, non reproduites |

La présence d’une colonne de raisonnement dans un schéma ne prouve pas que le chemin actif la remplit. De même, l’introduction prévue de workers justifie un contrat de nettoyage strict, mais ne permet pas d’affirmer qu’une course de threads GPU s’est déjà produite.

## Implicites désormais explicités

- **Contexte commun** : la base des deux rôles est identique, mais leurs systèmes et ajouts ne le sont pas ; une compaction tardive d’un seul rôle est interdite.
- **Préparation sans raisonnement brut** : le brief transporte but, contraintes et références validées, non un transcript d’orchestration ; les protections de format ne garantissent pas à elles seules l’absence de prompt injection.
- **Production hors chat** : le studio a un run de production et une portée de bibliothèque personnelle, sans créer de message utilisateur artificiel.
- **Annulation sans effacement** : un artefact committé reste visible après annulation ; un calcul encore vivant garde sa ressource.
- **Admission sans interblocage** : les sous-opérations réutilisent l’admission du run parent et ne redemandent pas la place qu’il occupe.
- **Un tour par conversation** : toute conversation n’a qu’un run de chat actif ; une seconde demande n’est pas insérée tant que le premier n’est pas terminal.
- **Maintenance sans course** : la collecte physique attend l’absence de publication ou rattachement actif avant de supprimer un blob.
- **Persistance sans livraison parfaite** : le terminal est logique et durable ; une coupure impose resynchronisation, pas une promesse de livraison réseau exactement une fois.
- **Suppression sans perte collatérale** : retirer un lien de conversation n’efface pas les octets encore référencés ailleurs.
- **Local sans fraîcheur présumée** : une source proche ou déjà lue peut être périmée ; le mode local strict interdit malgré tout le réseau.
- **Mémoire sans autorité automatique** : une synthèse est corrigeable, sourcée et scoped ; usage répété ne signifie pas vérité.
- **Déplacement sans fuite historique** : retirer une référence ne retire pas les extraits déjà présents dans un message ; un déplacement dépendant d’un ancien espace est refusé au profit d’un transfert sélectif autorisé.
- **Portable sans machine universelle** : le dossier est autonome pour un profil compatible et fermé avant copie ; OS, architecture, pilotes et politiques de l’hôte restent des conditions.
- **Accessibilité sans terminal imposé** : capacités manquantes, refus de permissions, contexte réduit et erreurs de lancement doivent être expliqués dans l’interface.

## Contrôles documentaires de livraison

La version initiale comportait 25 fichiers Markdown dans le périmètre README/documentation, avec 23 références locales cassées sur 93 liens examinés. Ce comptage a été reproduit sur le commit initial ; il ne concerne pas les URLs techniques externes.

Le contrôle final de la révision doit vérifier tous les fichiers et ancres locaux après achèvement des annexes et des modules, ainsi que l’absence de modification hors Markdown. Le résultat sera mis à jour ici avant commit ; aucune réussite applicative ne sera déduite de ce contrôle documentaire.

## Portes de sortie de préproduction

La prochaine étape n’est pas de coder les cinq phases simultanément. Elle consiste à clore les questions de socle qui changent la faisabilité : reproduire le profil LFM, qualifier les adaptateurs vision/OCR, construire un paquet autonome minimal et tester son arrêt/déplacement, selon [O01 à O07](decisions.md).

Le [plan d’acceptation](tests_acceptation.md) reste entièrement à exécuter côté application et matériel. Après chaque essai réel, enregistrer preuve, résultat et limite, puis mettre à jour le registre ; ne jamais transformer une hypothèse en promesse uniquement parce qu’elle apparaît dans plusieurs documents.
