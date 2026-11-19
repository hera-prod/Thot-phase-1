"""Forward Qwen3 natif sur tenseurs GGUF lazy — encodeur Klein (taps 9/18/27).

Conventions (validees empiriquement vs llama_cpp, voir scripts/probe_qwen_parity.py) :
- Poids lineaires : reshape taille `shape_pt` (= GGUF shape inversee) = [out, in],
  utilises DIRECTEMENT dans F.linear, SANS transpose supplementaire.
  (Note : `LazyGGUFTensor.dequantize()` transpose les 2D — ne pas l'utiliser ici.)
- token_embd : [vocab, hidden], lookup par index, sans transpose.
- RoPE standard base 1e6, head_dim 128, GQA 32Q/8KV, q_norm + k_norm par tete.
- TAPS = sorties des blocs d'indice 9/18/27 (0-based), concat -> [B,S,12288].
"""
import math
import torch
import torch.nn.functional as F
from gguf.quants import dequantize as dequantize_gguf


def _weight_pt(lazy) -> torch.Tensor:
    """Dequantifie un tenseur lazy en [out, in] (shape_pt), sans transpose."""
    import numpy as np
    qname = getattr(lazy.qtype, "name", str(lazy.qtype))
    if qname == "F32":
        arr = np.frombuffer(lazy.data, dtype=np.float32).astype(np.float32)
    elif qname == "F16":
        arr = np.frombuffer(lazy.data, dtype=np.float16).astype(np.float32)
    elif qname == "BF16":
        # numpy ne connait pas le bf16 : vue uint16 -> reinterpretation torch
        u16 = np.frombuffer(lazy.data, dtype=np.uint16).reshape(lazy.shape_pt)
        return torch.from_numpy(u16.copy()).view(torch.bfloat16)
    else:
        from gguf.quants import dequantize as dequantize_gguf
        arr = dequantize_gguf(lazy.data, lazy.qtype).astype(np.float32)
    t = torch.from_numpy(arr.reshape(lazy.shape_pt))
    del arr
    return t


def rms_norm(x: torch.Tensor, w: torch.Tensor, eps: float = 1e-6) -> torch.Tensor:
    xf = x.float()
    var = xf.pow(2).mean(dim=-1, keepdim=True)
    return (xf * torch.rsqrt(var + eps) * w.float()).to(x.dtype)


def build_rope_cache(seq_len: int, head_dim: int, base: float, device, dtype) -> torch.Tensor:
    theta = 1.0 / (base ** (torch.arange(0, head_dim, 2, device=device).float() / head_dim))
    pos = torch.arange(seq_len, device=device).float()
    freqs = torch.outer(pos, theta)  # [S, D/2]
    return torch.polar(torch.ones_like(freqs), freqs).to(dtype)  # complexe [S, D/2]


def apply_rope(x: torch.Tensor, cos_sin: torch.Tensor) -> torch.Tensor:
    # Convention NeoX (HF rotate_half) : paires (i, i+D/2), pas (2i, 2i+1).
    # x: [B, S, H, D] reel ; cos_sin: [S, D/2] complexe
    D = x.shape[-1]
    x1, x2 = x.float().chunk(2, dim=-1)  # [.., D/2] chacun
    x_c = torch.complex(x1, x2)
    out = x_c * cos_sin.unsqueeze(0).unsqueeze(2)
    return torch.cat([out.real, out.imag], dim=-1).to(x.dtype)


def repeat_kv(x: torch.Tensor, n_rep: int) -> torch.Tensor:
    # x: [B, S, Hkv, D] -> [B, S, Hkv*n_rep, D]
    if n_rep == 1:
        return x
    return x[:, :, :, None, :].expand(x.shape[0], x.shape[1], x.shape[2], n_rep, x.shape[3]).reshape(
        x.shape[0], x.shape[1], x.shape[2] * n_rep, x.shape[3])


class Qwen3NativeEncoder:
    """Forward Qwen3 complet, une couche a la fois (pic memoire ~1 tenseur FFN)."""

    # Sorties des blocs 8/17/26 (0-based) = hidden_states[9/18/27] HF (cf. pipeline
    # klein : hidden_states_layers=(9, 18, 27), [0] = embeddings).
    TAPS = (8, 17, 26)

    def __init__(self, loader, n_layers=36, n_q=32, n_kv=8, head_dim=128,
                 eps=1e-6, rope_base=1e6, device: torch.device | str = "cuda", dtype=torch.bfloat16):
        self.loader = loader
        self.n_layers = n_layers
        self.n_q = n_q
        self.n_kv = n_kv
        self.n_rep = n_q // n_kv
        self.hd = head_dim
        self.eps = eps
        self.rope_base = rope_base
        self.device = torch.device(device)
        self.dtype = dtype

    def _w(self, name: str) -> torch.Tensor:
        lazy = self.loader.get_lazy(name)
        assert lazy is not None, f"tenseur GGUF manquant: {name}"
        return _weight_pt(lazy).to(device=self.device, dtype=self.dtype)

    def _embed_matrix(self) -> torch.Tensor:
        w = self._w("token_embd.weight")
        return w

    def _fit_input_ids(self, input_ids: torch.Tensor, vocabulary_rows: int) -> torch.Tensor:
        if input_ids.numel() == 0:
            raise RuntimeError("[TOKEN_OUT_OF_RANGE] input_ids est vide.")
        minimum_token = int(input_ids.min().item())
        if minimum_token < 0:
            raise RuntimeError(
                f"[TOKEN_OUT_OF_RANGE] token id négatif (min={minimum_token})."
            )
        ids = input_ids.clone()
        invalid = ids >= vocabulary_rows
        if bool(invalid.any()):
            invalid_id = int(ids[invalid].max().item())
            raise RuntimeError(
                f"[TOKEN_OUT_OF_RANGE] Token {invalid_id} hors vocabulary du text encoder "
                f"({vocabulary_rows} lignes)."
            )
        return ids

    def forward_taps(self, input_ids: torch.Tensor, attention_mask: torch.Tensor | None = None):
        """input_ids [B,S] (device quelconque) -> (taps [B,S,12288], last [B,S,4096])."""
        dev, dt = self.device, self.dtype
        B, S = input_ids.shape
        w_emb = self._embed_matrix()
        vocabulary_rows = int(w_emb.shape[0])
        ids = self._fit_input_ids(input_ids, vocabulary_rows).to(dev)
        h = F.embedding(ids, w_emb).to(dt)
        del w_emb

        rope = build_rope_cache(S, self.hd, self.rope_base, dev, torch.complex64)
        scale = 1.0 / math.sqrt(self.hd)
        if attention_mask is None:
            attn_mask = None
        else:
            if attention_mask.ndim != 2 or attention_mask.shape != input_ids.shape:
                raise ValueError("attention_mask doit avoir la forme de input_ids")
            causal = torch.ones((S, S), dtype=torch.bool, device=dev).tril()
            valid_keys = attention_mask.to(device=dev, dtype=torch.bool)
            attn_mask = causal[None, None, :, :] & valid_keys[:, None, None, :]
        taps = {}

        for i in range(self.n_layers):
            p = f"blk.{i}."
            # --- attention ---
            h_n = rms_norm(h, self._w(p + "attn_norm.weight"), self.eps)
            q = F.linear(h_n, self._w(p + "attn_q.weight"))          # [B,S,4096]
            k = F.linear(h_n, self._w(p + "attn_k.weight"))          # [B,S,1024]
            v = F.linear(h_n, self._w(p + "attn_v.weight"))
            q = q.view(B, S, self.n_q, self.hd)
            k = k.view(B, S, self.n_kv, self.hd)
            v = v.view(B, S, self.n_kv, self.hd)
            q = rms_norm(q, self._w(p + "attn_q_norm.weight"), self.eps)
            k = rms_norm(k, self._w(p + "attn_k_norm.weight"), self.eps)
            q, k = apply_rope(q, rope), apply_rope(k, rope)
            k = repeat_kv(k, self.n_rep)
            v = repeat_kv(v, self.n_rep)
            attn = F.scaled_dot_product_attention(
                q.transpose(1, 2), k.transpose(1, 2), v.transpose(1, 2),
                attn_mask=attn_mask, is_causal=attn_mask is None, scale=scale,
            ).transpose(1, 2).reshape(B, S, -1)
            h = (h + F.linear(attn, self._w(p + "attn_output.weight"))).to(dt)
            # --- mlp ---
            h_n = rms_norm(h, self._w(p + "ffn_norm.weight"), self.eps)
            g = F.linear(h_n, self._w(p + "ffn_gate.weight"))
            u = F.linear(h_n, self._w(p + "ffn_up.weight"))
            h = (h + F.linear(F.silu(g) * u, self._w(p + "ffn_down.weight"))).to(dt)
            if i in self.TAPS:
                taps[i] = h.clone()
            if dev.type == "cuda":
                torch.cuda.empty_cache()

        last = rms_norm(h, self._w("output_norm.weight"), self.eps)
        out = torch.cat([taps[8], taps[17], taps[26]], dim=-1)
        return out, last
