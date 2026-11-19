#!/usr/bin/env python3
"""Stream-parser for GGUF v3 — inventories tensor names, shapes, quant types.
Uses gguf.GGUFReader with memory-mapped file access.
Usage: python scripts/extract_architecture.py models/transformer/flux2-klein-9b-kv-Q4_K_M.gguf
Output: outputs/tensor_inventory.txt
"""

import sys
from pathlib import Path
from datetime import datetime


# Mapping from ggml_quantization_type enum to display name
GGML_QTYPES = {
    0: 'F32', 1: 'F16', 2: 'I32', 3: 'I16', 4: 'I8', 5: 'U8',
    6: 'BOOL', 7: 'FP8_E5M2', 8: 'FP8_E4M3',
    9: 'Q2_K', 10: 'Q3_K', 11: 'Q4_K', 12: 'Q5_K', 13: 'Q6_K', 14: 'Q8_K',
    15: 'MQ_2_3', 16: 'MQ_2_4', 17: 'MQ_2_5', 18: 'MQ_2_6',
    19: 'MQ_3_4', 20: 'MQ_3_5', 21: 'MQ_3_6',
    22: 'IQ1_M', 23: 'IQ1_S', 24: 'IQ2_M', 25: 'IQ2_S', 26: 'IQ2_ML',
    27: 'IQ3_M', 28: 'IQ3_S', 29: 'IQ4_M', 30: 'IQ4_S',
}

# Bytes per element for non-quantized types
NORM_BYTES = {'F32': 4, 'F16': 2, 'I32': 4, 'I16': 2, 'I8': 1, 'U8': 1, 'BOOL': 1,
              'FP8_E5M2': 1, 'FP8_E4M3': 1}


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <model.gguf>", file=sys.stderr)
        sys.exit(1)

    gguf_path = Path(sys.argv[1])
    if not gguf_path.exists():
        print(f"[✗] File not found: {gguf_path}", file=sys.stderr)
        sys.exit(1)

    output_dir = Path(__file__).resolve().parent.parent / "outputs"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / "tensor_inventory.txt"

    file_size = gguf_path.stat().st_size
    print(f"[extract] Reading GGUF (memory-mapped): {gguf_path} ({file_size / 1e9:.2f} GB)")

    import gguf
    reader = gguf.GGUFReader(str(gguf_path), 'r')

    # Parse KV metadata from reader.fields
    kv_dict = {}
    for fname, field in reader.fields.items():
        if fname.startswith('GGUF.'):
            continue
        for part in field.parts:
            if hasattr(part, 'data') and len(part.data) > 0:
                try:
                    kv_dict[fname] = repr(part.data[0])
                except Exception:
                    pass

    # Parse tensor headers (no data loaded into memory for most readers)
    type_counts = {}
    tensors_info = []
    for i, tensor in enumerate(reader.tensors):
        tname = GGML_QTYPES.get(int(tensor.tensor_type), f'Type{tensor.tensor_type}')
        type_counts[tname] = type_counts.get(tname, 0) + 1
        dims = [int(d) for d in tensor.shape]
        tensors_info.append({
            'idx': i,
            'name': tensor.name,
            'dims': dims,
            'type_name': tname,
            'n_elements': int(tensor.n_elements),
            'n_bytes': int(tensor.n_bytes),
        })

    # Summary
    total_bytes = sum(t['n_bytes'] for t in tensors_info)
    n_float = sum(1 for t in tensors_info if t['type_name'] in NORM_BYTES)
    n_quant = sum(1 for t in tensors_info if t['type_name'] not in NORM_BYTES)

    print(f"  [ok]   Total tensors: {len(tensors_info)}")
    print(f"  [ok]   Float: {n_float}, Quantized: {n_quant}")
    print(f"  [ok]   Types: {dict(type_counts)}")
    print(f"  [ok]   Total size: {total_bytes / 1e4:.0f} KB ({total_bytes / 1e9:.2f} GB)")

    # Write output
    with open(output_file, 'w') as f:
        f.write(f"# Tensor Inventory — {gguf_path.name}\n")
        f.write(f"# Generated: {datetime.now().isoformat()}\n")
        f.write(f"# Total tensors: {len(tensors_info)}\n")
        f.write(f"# Float: {n_float}, Quantized: {n_quant}\n")
        f.write(f"# Total size (GGUF reader): {total_bytes:,} bytes ({total_bytes / 1e9:.4f} GB)\n\n")

        f.write("## KV Metadata\n")
        for k, v in kv_dict.items():
            f.write(f"  {k} = {v}\n")

        f.write("\n## Tensor Inventory\n")
        f.write(f"{'Idx':<6}{'Name':<55}{'Shape':<20}{'Type':<12}{'Elements':>12}{'Size':>12}\n")
        f.write("-" * 115 + "\n")

        for t in tensors_info:
            dims_str = 'x'.join(str(d) for d in t['dims'])
            sz = t['n_bytes']
            if sz >= 1e9:
                sz_str = f"{sz/1e9:.2f} GB"
            elif sz >= 1e6:
                sz_str = f"{sz/1e6:.2f} MB"
            elif sz >= 1e3:
                sz_str = f"{sz/1e3:.1f} KB"
            else:
                sz_str = f"{sz} B"
            f.write(f"{t['idx']:<6}{t['name']:<55}{dims_str:<20}{t['type_name']:<12}{t['n_elements']:>12}{sz_str:>12}\n")

        f.write("\n## Summary by type:\n")
        for ttype, count in sorted(type_counts.items(), key=lambda x: -x[1]):
            typ_bytes = sum(t['n_bytes'] for t in tensors_info if t['type_name'] == ttype)
            f.write(f"  {ttype}: {count} tensors, {typ_bytes/1e6:.1f} MB\n")

    print(f"[✓] Inventory written to {output_file}")

    # Terminal preview
    print(f"\n[Preview] First 40 tensors:")
    print(f"  {'Idx':<4} {'Name':<55} {'Shape':<18} {'Type':<12}")
    for t in tensors_info[:40]:
        dims_str = 'x'.join(str(d) for d in t['dims'])
        print(f"  {t['idx']:3d} {t['name']:<55} {dims_str:<18} {t['type_name']:<12}")
    if len(tensors_info) > 40:
        print(f"  ... and {len(tensors_info) - 40} more")


if __name__ == "__main__":
    main()
