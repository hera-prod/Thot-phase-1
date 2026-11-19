"""Dequant GPU — port torch de city96/ComfyUI-GGUF/dequant.py (rapport_lazy-dequant §2)."""
import torch
import gguf

QK_K = 256
K_SCALE_SIZE = 12


def to_uint32(x):
    x = x.view(torch.uint8).to(torch.int32)
    return (x[:, 0] | x[:, 1] << 8 | x[:, 2] << 16 | x[:, 3] << 24).unsqueeze(1)


def split_block_dims(blocks, *args):
    n_max = blocks.shape[1]
    dims = list(args) + [n_max - sum(args)]
    return torch.split(blocks, dims, dim=1)


def get_scale_min(scales):
    n_blocks = scales.shape[0]
    scales = scales.view(torch.uint8).reshape((n_blocks, 3, 4))
    d, m, m_d = torch.split(scales, scales.shape[-2] // 3, dim=-2)
    sc = torch.cat([d & 0x3F, (m_d & 0x0F) | ((d >> 2) & 0x30)], dim=-1)
    mn = torch.cat([m & 0x3F, (m_d >> 4) | ((m >> 2) & 0x30)], dim=-1)
    return sc.reshape((n_blocks, 8)), mn.reshape((n_blocks, 8))


def dequantize_blocks_Q6_K(blocks, block_size, type_size, dtype=None):
    n_blocks = blocks.shape[0]
    ql, qh, scales, d = split_block_dims(blocks, QK_K // 2, QK_K // 4, QK_K // 16)
    scales = scales.view(torch.int8).to(dtype)
    d = d.view(torch.float16).to(dtype)
    d = (d * scales).reshape((n_blocks, QK_K // 16, 1))
    ql = ql.reshape((n_blocks, -1, 1, 64)) >> torch.tensor([0, 4], device=d.device, dtype=torch.uint8).reshape((1, 1, 2, 1))
    ql = (ql & 0x0F).reshape((n_blocks, -1, 32))
    qh = qh.reshape((n_blocks, -1, 1, 32)) >> torch.tensor([0, 2, 4, 6], device=d.device, dtype=torch.uint8).reshape((1, 1, 4, 1))
    qh = (qh & 0x03).reshape((n_blocks, -1, 32))
    q = (ql | (qh << 4)).to(torch.int8) - 32
    q = q.reshape((n_blocks, QK_K // 16, -1))
    return (d * q).reshape((n_blocks, QK_K))


def dequantize_blocks_Q5_K(blocks, block_size, type_size, dtype=None):
    n_blocks = blocks.shape[0]
    d, dmin, scales, qh, qs = split_block_dims(blocks, 2, 2, K_SCALE_SIZE, QK_K // 8)
    d = d.view(torch.float16).to(dtype)
    dmin = dmin.view(torch.float16).to(dtype)
    sc, m = get_scale_min(scales)
    d = (d * sc).reshape((n_blocks, -1, 1))
    dm = (dmin * m).reshape((n_blocks, -1, 1))
    ql = qs.reshape((n_blocks, -1, 1, 32)) >> torch.tensor([0, 4], device=d.device, dtype=torch.uint8).reshape((1, 1, 2, 1))
    qh = qh.reshape((n_blocks, -1, 1, 32)) >> torch.tensor([i for i in range(8)], device=d.device, dtype=torch.uint8).reshape((1, 1, 8, 1))
    ql = (ql & 0x0F).reshape((n_blocks, -1, 32))
    qh = (qh & 0x01).reshape((n_blocks, -1, 32))
    q = (ql | (qh << 4))
    return (d * q - dm).reshape((n_blocks, QK_K))


def dequantize_blocks_Q4_K(blocks, block_size, type_size, dtype=None):
    n_blocks = blocks.shape[0]
    d, dmin, scales, qs = split_block_dims(blocks, 2, 2, K_SCALE_SIZE)
    d = d.view(torch.float16).to(dtype)
    dmin = dmin.view(torch.float16).to(dtype)
    sc, m = get_scale_min(scales)
    d = (d * sc).reshape((n_blocks, -1, 1))
    dm = (dmin * m).reshape((n_blocks, -1, 1))
    qs = qs.reshape((n_blocks, -1, 1, 32)) >> torch.tensor([0, 4], device=d.device, dtype=torch.uint8).reshape((1, 1, 2, 1))
    qs = (qs & 0x0F).reshape((n_blocks, -1, 32))
    return (d * qs - dm).reshape((n_blocks, QK_K))


def dequantize_blocks_Q8_0(blocks, block_size, type_size, dtype=None):
    d, x = split_block_dims(blocks, 2)
    d = d.view(torch.float16).to(dtype)
    x = x.view(torch.int8)
    return d * x


def _dequantize_blocks(blocks, block_size, type_size, dtype, fn):
    n_blocks = blocks.shape[0]
    oshape = (n_blocks, block_size)
    blocks = blocks.view(torch.uint8).reshape((n_blocks, type_size))
    out = fn(blocks, block_size, type_size, dtype)
    return out.reshape(oshape)


DEQUANT_GPU = {
    gguf.GGMLQuantizationType.Q6_K: dequantize_blocks_Q6_K,
    gguf.GGMLQuantizationType.Q5_K: dequantize_blocks_Q5_K,
    gguf.GGMLQuantizationType.Q4_K: dequantize_blocks_Q4_K,
    gguf.GGMLQuantizationType.Q8_0: dequantize_blocks_Q8_0,
}


def dequantize_gpu(raw_u8: torch.Tensor, qtype, shape_pt: tuple, dtype=torch.bfloat16) -> torch.Tensor:
    import numpy as np

    qname = getattr(qtype, "name", str(qtype))
    shape = tuple(int(dimension) for dimension in shape_pt)
    expected_elements = int(np.prod(shape, dtype=np.int64))
    try:
        if qname in ("BF16", "F16", "F32"):
            itemsize = {"BF16": 2, "F16": 2, "F32": 4}[qname]
            raw = raw_u8[:expected_elements * itemsize].contiguous()
            if raw.numel() != expected_elements * itemsize:
                raise ValueError("octets insuffisants pour le tenseur")
            if qname == "F32":
                tensor = raw.view(torch.float32)
            else:
                tensor = raw.view(torch.uint16).view(
                    torch.bfloat16 if qname == "BF16" else torch.float16
                )
            return tensor.reshape(shape).to(dtype=dtype)

        try:
            from gguf.constants import GGML_QUANT_SIZES
            block_size, type_size = GGML_QUANT_SIZES[qtype]
        except Exception:
            fallback = {
                gguf.GGMLQuantizationType.Q6_K: (256, 210),
                gguf.GGMLQuantizationType.Q5_K: (256, 176),
                gguf.GGMLQuantizationType.Q4_K: (256, 144),
                gguf.GGMLQuantizationType.Q8_0: (32, 34),
            }
            if qtype not in fallback:
                raise KeyError(f"type de quantification inconnu : {qtype}")
            block_size, type_size = fallback[qtype]
        block_size = int(block_size)
        type_size = int(type_size)
        n_blocks = int((expected_elements + block_size - 1) // block_size)
        blocks = raw_u8[:n_blocks * type_size].reshape((n_blocks, type_size))
        if blocks.numel() != n_blocks * type_size:
            raise ValueError("octets quantifiés insuffisants")
        function = DEQUANT_GPU.get(qtype)
        if function is None:
            raise LookupError(f"type de quantification non géré : {qtype}")
        output = _dequantize_blocks(blocks, block_size, type_size, dtype, function)
        if tuple(output.shape) != (n_blocks, block_size):
            raise ValueError("forme de déquantification GPU invalide")
        output = output.reshape(shape)
        if output.numel() != expected_elements:
            raise ValueError("nombre d'éléments GPU invalide")
        return output
    except torch.cuda.OutOfMemoryError:
        raise
    except Exception as gpu_exc:
        try:
            from gguf.quants import dequantize as dequant_cpu
            raw_bytes = raw_u8.cpu().numpy().tobytes()
            raw_array = np.frombuffer(raw_bytes, dtype=np.uint8)
            cpu_values = np.asarray(dequant_cpu(raw_array, qtype), dtype=np.float32)
            tensor = torch.from_numpy(cpu_values.copy()).to(
                dtype=dtype, device=raw_u8.device
            )
            if tensor.numel() != expected_elements:
                raise ValueError("nombre d'éléments CPU invalide")
            return tensor.reshape(shape)
        except Exception as cpu_exc:
            raise RuntimeError(
                f"[DEQUANT_FAILED] GPU: {gpu_exc}; CPU: {cpu_exc}"
            ) from cpu_exc
