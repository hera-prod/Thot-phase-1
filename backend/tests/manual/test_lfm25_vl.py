"""Smoke test LFM2.5-VL-3B hors orchestrateur — Réponses.md P0 étape 3"""
import base64
from pathlib import Path

import pytest

# Poids + mmproj VL ≈ 2.5 Go + contexte : exige de la VRAM réellement libre.
# Si l'app tourne à côté (8B résident ≈ 9 Go), le test est SKIPpé — pas FAILED :
# deux processus qui se battent pour un GPU n'est pas un verdict sur le code.
MIN_FREE_VRAM_MB = 6000


def test_lfm25_vl():
    from backend.inference.manager import ModelManager
    try:
        free_mb = ModelManager()._get_free_vram_mb()
    except Exception:
        free_mb = 0
    if free_mb < MIN_FREE_VRAM_MB:
        pytest.skip(f"VRAM insuffisante pour le smoke test ({free_mb} Mo libres, {MIN_FREE_VRAM_MB} requis — fermez l'app)")
    from backend.inference.loop import prepare_vision_image

    # Crée une image test 64x64 rouge si pas de fixture
    fixture = Path("tests/fixtures/red_square.png")
    fixture.parent.mkdir(parents=True, exist_ok=True)
    if not fixture.exists():
        from PIL import Image
        Image.new("RGB", (64, 64), (255, 0, 0)).save(fixture)

    # Prépare
    prepared = prepare_vision_image(fixture, Path("/tmp/thot/test_vl/prepared.jpg"))
    with open(prepared, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    data_url = f"data:image/jpeg;base64,{b64}"
    print(f"[test] fixture={fixture} prepared={prepared} b64_len={len(b64)}")

    mgr = ModelManager()
    with mgr.use_model("LFM2.5-VL-3B") as vlm:
        print(f"[test] model loaded, n_ctx should be 32768, trying chat...")
        try:
            r = vlm.create_chat_completion(
                messages=[{"role": "user", "content": [{"type": "image_url", "image_url": {"url": data_url}}, {"type": "text", "text": "Quelle est la couleur dominante de cette image ? Reponds en un mot."}]}],
                temperature=0.0,
                max_tokens=64,
            )
            text = r["choices"][0]["message"]["content"]
            print(f"[test] response: {text!r}")
            assert "rouge" in text.lower() or "red" in text.lower(), f"Attendu rouge/red, got {text!r}"
            assert "1885477" not in text
            print("[test] PASS — VLM voit l'image (tokens << 32768)")
        except Exception as e:
            msg = str(e)
            if "exceed context" in msg or "Requested tokens" in msg:
                print(f"[test] FAIL — base64 traite comme texte: {msg[:500]}")
                print("[test] Cause: mmproj/handler incompatible ou absent")
            raise

if __name__ == "__main__":
    test_lfm25_vl()
