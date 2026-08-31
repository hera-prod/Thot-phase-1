# Tests HardwareDetector — mock nvidia-smi (point critique Phase 1)
import sys
from unittest.mock import patch

def test_cuda_detection(monkeypatch):
    """Vérifie que nvidia-smi est bien parsé."""
    from backend.inference.hardware import HardwareDetector
    # On simule une sortie nvidia-smi
    fake_output = "NVIDIA GeForce RTX 4070 SUPER, 12282, 11500\n"
    with patch("backend.inference.hardware._run", return_value=fake_output):
        d = HardwareDetector()
        cfg = d.detect()
        assert cfg.backend == "cuda"
        assert cfg.total_vram > 0

def test_cpu_fallback():
    """Si aucun GPU, on tombe en CPU sans crasher."""
    from backend.inference.hardware import HardwareDetector
    with patch("backend.inference.hardware._run", return_value=None):
        d = HardwareDetector()
        cfg = d.detect()
        assert cfg.backend == "cpu"
        assert cfg.n_gpu_layers == 0

def test_estimate_layers():
    """La formule simplifiée ne doit jamais OOM."""
    from backend.inference.hardware import estimate_layers
    MB = 1024*1024
    # 8GB free, modèle 5GB, KV 1GB, 24 couches → doit donner ~partiel
    layers = estimate_layers(8*1024*MB, 5*1024*MB, 1*1024*MB, 24)
    assert 0 <= layers <= 24
