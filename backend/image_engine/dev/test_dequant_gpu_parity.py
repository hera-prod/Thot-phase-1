"""Gate parite: GPU vs CPU dequant <1e-3 (rapport_lazy-dequant §2)."""
import torch
import numpy as np
from gguf import GGUFReader
import gguf
from gguf.quants import dequantize as dequant_cpu
from pathlib import Path

# Import GPU dequant
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # racine projet (paquet `backend`)
from backend.image_engine.core.paths import MODELS_DIR  # models/flux2-klein
from backend.image_engine.loaders.dequant_gpu import dequantize_gpu
from backend.image_engine.loaders.raw_vram import RawVRAMIndex

gguf_path = str(MODELS_DIR / "transformer/flux2-klein-9b-kv-Q4_K_M.gguf")
reader = GGUFReader(gguf_path)

# Types a tester
types_to_test = {
    gguf.GGMLQuantizationType.Q6_K: [],
    gguf.GGMLQuantizationType.Q5_K: [],
    gguf.GGMLQuantizationType.Q4_K: [],
    gguf.GGMLQuantizationType.F32: [],
}

# Collecte 20 tenseurs par type
for t in reader.tensors:
    qt = t.tensor_type
    if qt in types_to_test and len(types_to_test[qt]) < 20:
        types_to_test[qt].append(t)

print(f"Testing parity GPU vs CPU (gate <1e-3)")
all_ok = True
for qtype, tensors in types_to_test.items():
    if not tensors:
        continue
    print(f"\n{qtype.name}: {len(tensors)} tensors")
    for t in tensors:
        shape_pt = tuple(reversed(t.shape))
        # CPU dequant (reference)
        cpu_arr = dequant_cpu(t.data, qtype).astype(np.float32)
        cpu_t = torch.from_numpy(cpu_arr.reshape(shape_pt))
        # GPU dequant via RawVRAMIndex (or direct)
        # Simulate raw_u8 on GPU
        raw_u8 = torch.from_numpy(np.frombuffer(t.data, dtype=np.uint8).copy()).to("cuda" if torch.cuda.is_available() else "cpu")
        try:
            gpu_t = dequantize_gpu(raw_u8, qtype, shape_pt, dtype=torch.float32)
            gpu_t = gpu_t.cpu()
            # Compare
            max_abs_cpu = cpu_t.abs().max().item() + 1e-8
            diff = (gpu_t - cpu_t).abs().max().item()
            rel = diff / max_abs_cpu
            ok = rel < 1e-3
            status = "✅" if ok else "❌"
            print(f"  {status} {t.name[:50]:50} max_abs {max_abs_cpu:.3f} diff {diff:.6f} rel {rel:.6f} {'PASS' if ok else 'FAIL'}")
            if not ok:
                all_ok = False
        except Exception as e:
            print(f"  ⚠️  {t.name[:50]} error: {e}")
            # Fallback still counts as ok if CPU works
            import traceback
            traceback.print_exc()
            all_ok = False

if all_ok:
    print("\n✅ Gate parite PASS (<1e-3) pour tous les types")
else:
    print("\n❌ Gate parite FAIL")

# Also test RawVRAMIndex
print("\n=== RawVRAMIndex test ===")
if torch.cuda.is_available():
    idx = RawVRAMIndex(gguf_path, device="cuda")
    print(f"RawVRAMIndex: {len(idx.raw)} tensors, VRAM {sum(v.numel() for v in idx.raw.values())/1024**3:.2f} Go (raw uint8)")
    # Check one
    name = list(idx.raw.keys())[2]
    print(f"  Sample {name}: {idx.raw[name].shape} {idx.raw[name].dtype} on {idx.raw[name].device} meta {idx.meta[name]}")
    # Test dequant of one
    raw = idx.raw[name]
    shape, qname = idx.meta[name]
    import gguf
    qtype = getattr(gguf.GGMLQuantizationType, qname)
    try:
        w = dequantize_gpu(raw, qtype, shape, dtype=torch.bfloat16)
        print(f"  Dequant {name} -> {w.shape} {w.dtype} mean {w.float().mean():.4f} (GPU)")
    except Exception as e:
        print(f"  Dequant error: {e}")
        import traceback
        traceback.print_exc()
    idx.release()
    print(f"After release: {len(idx.raw)} tensors, empty_cache done")
    # Check RssAnon
    import os, psutil
    anon = 0
    with open(f"/proc/{os.getpid()}/status") as f:
        for line in f:
            if line.startswith("RssAnon:"):
                anon = int(line.split()[1])/1024
    print(f"RssAnon after release: {anon:.0f} MB (cible ≤3072)")
else:
    print("No CUDA, skip RawVRAMIndex VRAM test")
