# scripts/derive_klein_constants.py — historique (constantes figées depuis).
import re
from pathlib import Path

inv_path = Path(__file__).resolve().parents[2] / "docs" / "depot" / "flux2-klein" / "outputs" / "tensor_inventory.txt"
tensors = {}
sh = {}
with open(inv_path) as f:
    for line in f:
        if line.startswith("#") or line.startswith("-") or line.strip() == "" or line.startswith("Idx") or line.startswith("##"):
            continue
        # Parse: idx name shape type elements size
        parts = line.split()
        if len(parts) < 5:
            continue
        try:
            idx = int(parts[0])
            name = parts[1]
            shape = parts[2]
            tensors[name] = shape
            # Parse dims
            dims = [int(x) for x in shape.split("x") if x.isdigit() or x.replace("x","").isdigit()]
            sh[name] = dims
        except:
            continue

def get_shape(name):
    return sh.get(name, [])

# Deduce constants
def dims_of(name):
    return sh.get(name, [])

print("hidden      =", dims_of("double_blocks.0.img_attn.proj.weight")[0] if "double_blocks.0.img_attn.proj.weight" in sh else "N/A")  # 4096
# qkv
if "double_blocks.0.img_attn.qkv.weight" in sh:
    print("qkv_fused   =", dims_of("double_blocks.0.img_attn.qkv.weight")[0])  # 12288 = 3*4096
    print("qkv_in      =", dims_of("double_blocks.0.img_attn.qkv.weight")[1])
# mlp
if "double_blocks.0.img_mlp.0.weight" in sh:
    print("mlp_0       =", dims_of("double_blocks.0.img_mlp.0.weight"))  # [4096,24576]
    print("mlp_hidden  =", dims_of("double_blocks.0.img_mlp.0.weight")[0] // 2 if len(dims_of("double_blocks.0.img_mlp.0.weight"))==2 else "N/A")
if "txt_in.weight" in sh:
    print("txt_in      =", dims_of("txt_in.weight"))  # [4096,12288]
# counts
n_double = max(int(n.split(".")[1]) for n in tensors if n.startswith("double_blocks"))+1 if any(n.startswith("double_blocks") for n in tensors) else 0
n_single = max(int(n.split(".")[1]) for n in tensors if n.startswith("single_blocks"))+1 if any(n.startswith("single_blocks") for n in tensors) else 0
print("n_double    =", n_double)
print("n_single    =", n_single)
print("total tensors =", len(tensors))
# Other
for k in ["time_in.in_layer.weight","time_in.out_layer.weight","single_stream_modulation.lin.weight","txt_in.weight","img_in.weight","final_layer.weight"]:
    if k in sh:
        print(f"{k} =", sh[k])
