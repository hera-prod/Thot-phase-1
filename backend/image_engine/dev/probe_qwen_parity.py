"""Parite encodeur natif vs llama_cpp (reference locale, meme GGUF, zero download)."""
import sys
import torch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # racine projet (paquet `backend`)
from backend.image_engine.core.paths import MODELS_DIR  # models/flux2-klein
from backend.image_engine.loaders.gguf_loader import Flux2KleinGGUFLoader
from backend.image_engine.models.qwen3_forward import Qwen3NativeEncoder

GGUF = str(MODELS_DIR / "text_encoder/Qwen3-8B-Q4_K_M.gguf")
PROMPTS = ["a cute cat sitting in a garden", "a medieval castle at sunset"]

def main():
    from tokenizers import Tokenizer
    tok = Tokenizer.from_file(str(MODELS_DIR / "tokenizer/tokenizer.json"))
    batch = tok.encode_batch(PROMPTS)
    maxlen = max(len(b.ids) for b in batch)
    ids = torch.tensor([b.ids + [0] * (maxlen - len(b.ids)) for b in batch])

    loader = Flux2KleinGGUFLoader(GGUF)
    enc = Qwen3NativeEncoder(loader)
    with torch.no_grad():
        taps, last = enc.forward_taps(ids)
    print("taps", tuple(taps.shape), "last", tuple(last.shape))

    # Reference llama_cpp (final-layer embeddings, meme fichier GGUF)
    from llama_cpp import Llama
    llm = Llama(model_path=GGUF, embedding=True, n_ctx=128, verbose=False)
    for j, p in enumerate(PROMPTS):
        ref = torch.tensor(llm.embed(p)).float()  # pooling LAST (metadata pooling_type=3)
        llama_ids = llm.tokenize(p.encode(), add_bos=False)
        print(f"prompt{j}: miens={batch[j].ids} llama={llama_ids}")
        ids2 = torch.tensor([llama_ids])
        with torch.no_grad():
            _, last2 = enc.forward_taps(ids2)
        got = last2[0, -1].float().cpu()
        cos = torch.nn.functional.cosine_similarity(got.flatten(), ref.flatten(), dim=0).item()
        print(f"prompt{j}: meme sequence -> cos={cos:.6f}")

if __name__ == "__main__":
    main()
