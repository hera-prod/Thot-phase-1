# Tests backend — doctrine anti-régression (lire avant d'ajouter un test)

> Leçon de `q-r.md` : une image analysée avec succès (vision 186 car.) a fini en
> « Je ne vois pas d'image ». Aucun test ne couvrait la plomberie des preuves
> (`obs_id` jamais transmis au brief), aucun ne simulait un « Coucou ! » sans outil,
> aucun ne gardait l'UI sans jargon. Ce dossier existe pour que ça n'arrive plus.

## Règle n° 1 : tout bug prod devient un test

1. Reproduire le cas réel en pur (sans modèle, sans GPU) dans
   `test_qr_regression.py` ou le fichier de spec concerné.
2. Nommer le test d'après le symptôme : `test_brief_refuse_artifact_id_...`,
   pas `test_fix_42`.
3. Citer la source dans le docstring : spec `01/02/03` (`docs/contrats/`), fichier `docs/depot/...`,
   ou log `q-r.md`.

## Règle n° 2 : ce que « bon » veut dire ici

- **Purs d'abord** : validateurs pydantic, rendus texte, helpers de décision
  (`is_chitchat`, `plan_media_tools`, `gate_from_regions`, `naturalize_row`...).
  Rapides (< 1 s), déterministes, sans DB si possible.
- **DB jetable ensuite** : SQLite `tmp_path` + `monkeypatch` (voir `tmp_repo`
  dans `test_media_pipeline.py` / `test_qr_regression.py`). Jamais la vraie DB.
- **Jamais de modèle/GPU/réseau** : les tests `manual/` sont exclus du run
  courant (`-k "not slow"`, `--ignore=tests/backend/manual`).
- **Un comportement, une assertion lisible** : le message d'erreur doit dire
  quel contrat spec est violé et quelle action est attendue.

## Matrice : qui couvre quoi (specs 01-02-03)

| Fichier | Pipelines / contrats verrouillés |
|---|---|
| `test_specs_01_02_03.py` | **Référence** : constantes, 14 codes, gate à confiance, boucle 12/9/4/8/4, chunks 500 tokens, tableur, 4 outils (validation + rendus + modes), brief + repli |
| `test_qr_regression.py` | Cas réels prod : preuve `obs_` citable, `art_xxx` refusé, bavardage zéro-outil, prompts sans jargon, UI sans « Intention/Outil retenu » |
| `test_compaction_situation.py` | **Mise en situation C06** : écriture jusqu'au budget de compaction (fenêtre réelle − 20 000), déclenchement, qualité du résumé (faits clés), 15K récents intacts, prune 40K, panne LLM sans perte de faits, persistance `ContextSnapshot` |
| `test_annexe12.py` | Parser, validation brief, sanitizer, anti-hallucination vision |
| `test_prompt_orchestrateur.py` | Prompt de l'orchestrateur : outils nommés, arguments documentés, `doc_N`, pas de forme copiable |
| `test_media_pipeline.py` | Registre, routage déterministe, `art_xxx` résolu, follow-ups, chemins d'échec sans manager |
| `test_media_policy.py` / `test_ocr_policy.py` | Politique vision/doc_ocr, requêtes factuelles |
| `test_ingestion_gate.py` | Heuristiques gate pures + worker gardé GPU (`slow`) |
| `test_search.py` / `test_search_bc.py` | FTS, scope conversation, fiches, compat |
| `test_trace.py` | Persistance bulle UI (thinking/plan/brief, backfill `msg_id`) |
| `test_phase_remise_niveau.py` | **Remise à niveau 18/09** : flags annulation C09, empreinte C11, `MAX_ITER` canonique 12, `distribution.json` valide |
| `test_loop_sanitizer.py` | Strip `<think>`, appels textuels, off-topic |
| `test_manager*.py`, `test_hardware.py` | Runtimes, VRAM, OOM — pas les contrats métier |

Si tu ajoutes un comportement spec, ajoute-le dans `test_specs_01_02_03.py`.
Si tu corriges un bug vu en prod, ajoute-le dans `test_qr_regression.py`.

## Checklist avant chaque modification

```bash
python3 -m pytest tests/backend/test_qr_regression.py tests/backend/test_specs_01_02_03.py -q
python3 -m pytest tests/backend/ -q --ignore=tests/backend/manual -k "not slow"
```

- Nouveau code → nouveau test **dans le même commit**.
- Test rouge d'abord (il échoue sans le fix), vert ensuite.
- Pas de littéral recopié : les seuils viennent de `backend/config/defaults.py`
  (`TOOL_LIMITS`), jamais en dur dans le test — sauf l'attendu spec lui-même
  (ex. `150`, `500`, `0.3`, `12/9/4/8/4`).
- Français pour les noms de cas quand le contrat est métier
  (« bavardage », « preuve du tour ») ; anglais technique pour le reste.

## Anti-patterns (revue systématique)

- ❌ Tester le mock au lieu du contrat (ex. `assert mock_called` sans vérifier le code d'erreur rendu au modèle).
- ❌ Test qui passe avec n'importe quelle valeur (pas d'assertion sur `EVIDENCE_UNKNOWN` vs `BAD_ARGS`).
- ❌ `time.sleep`, ports, GPU, réseau dans un test courant.
- ❌ Nouveau seuil en dur dans `backend/` sans l'ajouter à `TOOL_LIMITS` + test.
- ❌ Texte UI avec jargon interne (« intention », « media_available », nom de modèle)
  sans test `test_ui_sans_jargon_interne` mis à jour.

## Améliorer en continu

- Chaque relecture de `q-r.md` ou des logs prod doit se terminer par :
  « quel test aurait attrapé ça ? » — puis l'écrire.
- Quand un seuil `PROVISOIRE` (ex. `pdf_sync 20 / pdf_batch 100`) est mesuré sur
  le matériel cible, mettre à jour `defaults.py` **et** l'attendu du test.
- Les poids lourds (vrai ColBERT `MaxSim`, `PP-DocLayoutV3` ONNX,
  `pdfplumber/Camelot`) restent derrière des interfaces pures et testées
  (`filter_regions_by_confidence`, `naturalize_row`, `validate_table`) :
  le jour du branchement, seuls les adaptateurs changent, les contrats restent verts.
