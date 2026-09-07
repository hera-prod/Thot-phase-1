# Annexe 04 — Audio embarqué (dictée et lecture vocale)

Sources récupérées le **2026-09-06**. Conventions **Fait / Décision Thot / À mesurer** : voir le [sommaire des annexes](README.md).

## Reconnaissance vocale (STT)

### whisper.cpp

**Fait.** « Les modèles sont multilingues sauf si leur nom contient `.en` » ([whisper.cpp — models/README.md](https://raw.githubusercontent.com/ggml-org/whisper.cpp/master/models/README.md)).

**Fait.** Tailles sur disque publiées par ce même fichier : tiny **75 MiB**, base **142 MiB**, small **466 MiB**, medium **1,5 GiB**, large-v3 **2,9 GiB**, large-v3-turbo **1,5 GiB**, large-v3-turbo-q5_0 **547 MiB** ([models/README.md](https://raw.githubusercontent.com/ggml-org/whisper.cpp/master/models/README.md)). Variantes quantifiées supplémentaires listées côté dépôt de poids : base-q5_1 **57 MiB**, base-q8_0 **78 MiB**, small-q5_1 **181 MiB** ([Hugging Face ggerganov/whisper.cpp](https://huggingface.co/ggerganov/whisper.cpp)).

**Fait.** Les tailles mémoire et vitesses relatives publiées par OpenAI (tiny ~1 Go à large ~10 Go de VRAM) sont mesurées **sur GPU A100 et en anglais**, et la page précise que la vitesse varie fortement selon la langue et le débit de parole ([openai/whisper](https://github.com/openai/whisper)).

**À mesurer.** Aucune source ne fournit de latence CPU, d'empreinte RAM CPU ni de taux d'erreur en français : ces valeurs sont `n.a.` et doivent être obtenues par test. La révision précédente annonçait « base ~150–250 Mo » et une latence « imperceptible » : ces deux affirmations sont retirées.

### Moonshine

**Fait.** Le model card ne documente que des modèles **anglais** — tiny (**27 M** paramètres) et base (**61 M**) — décrits comme transcrivant de la parole anglaise en texte anglais ; aucun modèle multilingue n'y figure ([Moonshine — model-card.md](https://github.com/moonshine-ai/moonshine/blob/main/model-card.md)). Licence des poids et disponibilité ONNX : `n.a.` sur cette page.

**Décision Thot.** Moonshine ne peut donc pas être le moteur de dictée d'une interface française. La révision précédente le présentait comme le choix par défaut : la décision est **rouverte** dans [decisions.md](../decisions.md), avec whisper.cpp multilingue comme candidat principal pour le français.

## Synthèse vocale (TTS)

### Piper

**Fait.** Le dépôt historique `rhasspy/piper` est **archivé et en lecture seule depuis le 6 octobre 2025**, sous licence **MIT**, et renvoie explicitement le développement vers un nouveau dépôt ([github.com/rhasspy/piper](https://github.com/rhasspy/piper)).

**Fait.** Le dépôt de développement actuel est sous **GPL-3.0** et embarque espeak-ng pour la phonémisation ([OHF-Voice/piper1-gpl](https://github.com/OHF-voice/piper1-gpl)).

**Fait.** Six voix `fr_FR` sont listées : `gilles`, `mls`, `mls_1840`, `siwis`, `tom`, `upmc` ([VOICES.md du dépôt archivé](https://github.com/rhasspy/piper/blob/master/VOICES.md)). La licence propre à chaque voix est `n.a.` ici et doit être vérifiée voix par voix.

### Kokoro

**Fait.** Kokoro-82M compte **82 M de paramètres**, ses poids sont annoncés « Apache-licensed », et la version 1.0 comprend **54 voix** ([hexgrad/Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M)).

**Fait.** Le français ne compte qu'**une seule voix**, `ff_siwis` ([VOICES.md Kokoro](https://huggingface.co/hexgrad/Kokoro-82M/blob/main/VOICES.md)).

**Fait.** Tailles des exports ONNX : fp32 **326 MB**, fp16 **163 MB**, quantifié 8 bits **92,4 MB**, q8f16 **86 MB**, uint8 **177 MB**, uint8f16 **114 MB** ([onnx-community/Kokoro-82M-v1.0-ONNX](https://huggingface.co/onnx-community/Kokoro-82M-v1.0-ONNX)). La licence de ce dépôt ONNX est `n.a.`, et aucune empreinte RAM ni latence n'y figure.

**Correction.** L'affirmation antérieure « Kokoro ~150 Mo » et « voix françaises » (au pluriel) est retirée : la taille dépend de l'export choisi, et une seule voix française est documentée.

## Conséquences pour Thot

**Décision Thot.**

- Le français est traité comme une **capacité à qualifier séparément** pour l'interface, le modèle texte, l'OCR et les voix, conformément au [contrat de portabilité](../portabilite.md).
- La licence du moteur TTS est un critère de sélection au même rang que la qualité : **GPL-3.0** (Piper actuel) et **Apache** (poids Kokoro) n'imposent pas les mêmes obligations à une application distribuée. Arbitrage à consigner dans [decisions.md](../decisions.md) avant embarquement.
- Les quotas CPU/RAM de l'audio ([C07](../contrats_transversaux.md)) sont dimensionnés sur des mesures, pas sur les tailles de fichiers ci-dessus.
- La dictée reste une entrée utilisateur modifiable ([C14](../contrats_transversaux.md)) : aucune promesse de transcription continue n'est faite avant mesure.

**À mesurer** : RAM et latence de whisper.cpp base/large-v3-turbo-q5_0 en français sur les CPU cibles ; qualité perçue des voix `fr_FR` Piper contre `ff_siwis` Kokoro ; poids total de la pile audio embarquée ; comportement micro + lecture simultanés.
