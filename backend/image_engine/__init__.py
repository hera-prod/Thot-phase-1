"""Moteur image local FLUX.2-klein-9B (ex-dossier Flux2-Klein-9b, intégré au projet le 23/09).

Contenu : pipeline T2I lazy <8 Go (transformer Q4 phasé + encodeur Qwen3 natif + VAE).
Entrée production : `spike_t2i.py` (`python3 -m backend.image_engine.spike_t2i`),
invoqué en sous-processus par `backend/services/flux_spike.py` (admission C07, timeout,
publication atomique). `dev/` = diagnostics et entrées historiques (I2I run.py, probes).
"""
