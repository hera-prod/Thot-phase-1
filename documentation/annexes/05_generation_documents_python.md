# Annexe 05 — Génération et lecture de documents

Sources récupérées le **2026-09-06**. Conventions **Fait / Décision Thot / À mesurer** : voir le [sommaire des annexes](README.md).

## CSV : l'injection de formules est le risque principal

**Fait.** L'injection CSV, aussi appelée « Formula Injection », survient lorsqu'une cellule commençant par `=` est interprétée comme une formule par un tableur ; les conséquences documentées vont du détournement de la machine de la victime à l'exfiltration du contenu des feuilles ouvertes ([OWASP — CSV Injection](https://owasp.org/www-community/attacks/CSV_Injection)).

**Fait.** Une cellule ne doit pas commencer par **`=`**, **`+`**, **`-`**, **`@`**, la tabulation (0x09), le retour chariot (0x0D) ou le saut de ligne (0x0A), ni par les variantes pleine largeur **`＝ ＋ － ＠`** ; OWASP précise en outre qu'il **ne suffit pas** de contrôler le premier caractère, car les séparateurs (`,` `;`) et les guillemets (`"` `'`) permettent d'ouvrir une nouvelle cellule ([OWASP — CSV Injection](https://owasp.org/www-community/attacks/CSV_Injection)). Le même test figure dans le guide de tests d'OWASP ([WSTG — Testing for CSV Injection](https://owasp.org/www-project-web-security-testing-guide/latest/4-Web_Application_Security_Testing/07-Input_Validation_Testing/21-Testing_for_CSV_Injection)).

**Décision Thot.** Un écrivain CSV conforme garantit la **syntaxe**, pas l'**innocuité en tableur** : la validation de `generate_file` ([C13](../contrats_transversaux.md)) comprend donc une neutralisation explicite des préfixes ci-dessus, appliquée à chaque cellule, contenu généré par le modèle inclus.

## PDF : trois bibliothèques, trois régimes de licence

| Bibliothèque | Fait (licence) | Source |
| :--- | :--- | :--- |
| **reportlab** | Licence **BSD**, Python ≥ 3.9 | [PyPI — reportlab](https://pypi.org/project/reportlab/) |
| **fpdf2** | Licence **LGPL-3.0** | [Dépôt py-pdf/fpdf2](https://github.com/py-pdf/fpdf2), [texte de licence](https://github.com/py-pdf/fpdf2/blob/master/LICENSE) |
| **PyMuPDF** | Double licence : **AGPL open source ou licence commerciale** (Artifex étant l'agent commercial exclusif de MuPDF) | [PyMuPDF — About](https://pymupdf.readthedocs.io/en/latest/about.html) |

**Décision Thot.** La pile de génération PDF est choisie d'abord sur la licence, ensuite sur les fonctionnalités : **BSD** est le régime le moins contraignant pour une application distribuée, **LGPL-3.0** impose des obligations sur la bibliothèque elle-même, et **AGPL** exige soit la conformité complète, soit l'achat d'une licence commerciale. Cet arbitrage doit être clos avant préproduction et consigné dans [decisions.md](../decisions.md), avec archivage sous `licenses/`.

## Lecture de PDF : texte natif avant OCR

**Fait.** PyMuPDF extrait le texte tel qu'il est codé dans le document, avec des variantes fournissant des positions de blocs et de mots ([PyMuPDF — Text recipes](https://pymupdf.readthedocs.io/en/latest/recipes-text.html)).

**Fait.** La même page avertit que l'extraction native peut produire un **ordre de lecture inattendu** et des sauts de ligne non voulus, l'ordre dépendant du producteur du PDF ([PyMuPDF — Text recipes](https://pymupdf.readthedocs.io/en/latest/recipes-text.html)).

**Décision Thot.** Conformément à [C12](../contrats_transversaux.md), la lecture tente d'abord le texte natif et ne déclenche l'OCR que sur les pages sans texte exploitable, ou lorsque la mise en page/les tableaux le justifient. Le fait qu'un texte soit extrait n'implique pas qu'il soit dans le bon ordre : la reconstruction par positions est une piste **à mesurer** sur les documents réels de test.

**À mesurer / `n.a.`** : dépendances et prérequis exacts du chemin OCR de PyMuPDF (moteur externe, variables d'environnement) — non confirmés par une page récupérée ; qualité d'ordre de lecture sur le corpus de test Thot ; coût mémoire du rendu de pages en lots bornés.

## Rappel de portée

**Fait de conception, pas de source.** Un PDF ouvrable n'implique pas des chiffres justes : [C13](../contrats_transversaux.md) distingue validité technique, fidélité aux données et exactitude factuelle. Les annexes ne fournissent aucun exemple de code exécutable ; elles justifient les règles de validation.
