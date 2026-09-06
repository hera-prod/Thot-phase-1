# Annexe 05 — Génération de documents en Python

Cette fiche recense les bibliothèques fiables pour **générer des fichiers** (CSV, PDF, Markdown) côté Python, utilisées en **phase 3**. Le principe général : le LLM produit du **contenu structuré**, Python le **matérialise**.

---

## 1. CSV

Utiliser le **module standard `csv`** : aucune dépendance, sortie garantie valide.

- `csv.writer` pour écrire des lignes depuis une liste de listes.
- Penser à définir le délimiteur et l'encodage (souvent `utf-8-sig` pour Excel).

## 2. PDF

| Bibliothèque | Orientation |
| :--- | :--- |
| **fpdf2** | simple, légère, orientée dessin pas-à-pas |
| **reportlab** | riche, mise en page avancée (paragraphes, tableaux, flux) |

- `fpdf2` convient aux rapports simples (titres, paragraphes, tableaux basiques).
- `reportlab` (avec `platypus`) convient aux mises en page complexes et au multi-pages automatique.

## 3. Markdown

Le Markdown n'est que du **texte** : écrire la chaîne produite par le modèle dans un fichier `.md` suffit. Aucune bibliothèque requise.

## 4. Lecture / extraction (utile pour la recherche, phase 4)

- **PyMuPDF** (`pymupdf`) : lecture de PDF, rendu des pages en image (utile pour l'OCR page par page), extraction de texte.
- **trafilatura** : extraction du contenu principal d'une page web (nettoyage HTML → texte).
- **BeautifulSoup** : parsing HTML générique.

## 5. Bonnes pratiques de génération

- **Jamais de base64 ni de flux binaire venant du LLM** : le modèle émet des champs texte (lignes, titre, paragraphes), Python les assemble.
- **Valider les entrées** : nombre de colonnes, encodage, chemins de sortie.
- **Renvoyer un chemin + métadonnées** à l'interface, pas le contenu du fichier.

---

## Sources

- fpdf2 — documentation : https://py-pdf.github.io/fpdf2/
- reportlab — documentation : https://docs.reportlab.com/
- PyMuPDF — documentation : https://pymupdf.readthedocs.io/
- trafilatura — dépôt : https://github.com/adbar/trafilatura