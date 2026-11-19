"""FLUX.2 Klein Transformer — 8 double + 24 single, natif GGUF, lazy par bloc (v3 §2)."""

"""Transformer FLUX.2 Klein — architecture conforme diffusers transformer_flux2.py.

Conventions verifiees contre la source installee + config du checkpoint klein-9b-kv :
- Double bloc : attention JOINT [txt;img] (qkv separes img/txt, concat, RoPE joint,
  SDPA joint, split, proj separees). QK-norms = RMSNorm 128 (pas LayerNorm).
- Modulation : Linear(SiLU(temb)), chunk (shift, scale, gate) — cf. Flux2Modulation.
- RoPE : entrelace (repeat_interleave_real), theta 2000, axes (32,32,32,32).
- Timestep : echelle [0,1] (pipeline fait timestep/1000), sinusoidal [cos,sin]
  (flip_sin_to_cos=True), downscale 0. PAS de guidance (guidance_embeds=False).
- Sortie : AdaLayerNormContinue (norm + scale/shift de SiLU(temb)) puis proj_out.
- eps = 1e-6 partout.
"""
import torch
import torch.nn as nn
import torch.nn.functional as F

ROPE_AXES = (32, 32, 32, 32)
ROPE_THETA = 2000.0
N_HEADS = 32
HEAD_DIM = 128
EPS = 1e-6


def build_rope_joint(ids: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
    """ids [S,4] (t,h,w,l) -> (cos, sin) [S,128]. Interpole, cf. get_1d_rotary_pos_embed."""
    cos_out, sin_out = [], []
    pos = ids.float()
    for i, dim in enumerate(ROPE_AXES):
        freqs = 1.0 / (
            ROPE_THETA
            ** (torch.arange(0, dim, 2, dtype=torch.float64, device=pos.device) / dim)
        )
        f = torch.outer(pos[:, i].to(torch.float64), freqs)  # [S, dim/2]
        c = f.cos().repeat_interleave(2, dim=1)
        s = f.sin().repeat_interleave(2, dim=1)
        cos_out.append(c)
        sin_out.append(s)
    return torch.cat(cos_out, dim=-1).float(), torch.cat(sin_out, dim=-1).float()


def apply_rope_interleaved(
    x: torch.Tensor, cos: torch.Tensor, sin: torch.Tensor
) -> torch.Tensor:
    """x [B,S,H,D] ; cos/sin [S,D]. Paires (2i,2i+1), cf. apply_rotary_emb flux."""
    xr, xi = x.float().reshape(*x.shape[:-1], -1, 2).unbind(-1)
    rot = torch.stack([-xi, xr], dim=-1).flatten(-2)
    c = cos.to(x.device).unsqueeze(0).unsqueeze(2)
    s = sin.to(x.device).unsqueeze(0).unsqueeze(2)
    return (x.float() * c + rot * s).to(x.dtype)


from typing import Dict, Optional, Tuple, cast

# Forward de reference copie depuis extracted_diffusers/forward.txt :
# Flux2TransformerBlock: modulation -> qk-norm -> RoPE -> _flux2_kv_causal_attention -> concat
# Flux2SingleTransformerBlock: parallel attn+mlp (linear1 36864, linear2 16384)


class Flux2KleinDoubleBlock(nn.Module):
    """Double block natif (12 tenseurs) — img_attn/txt_attn qkv/proj + mlp SwiGLU."""

    def __init__(self, hidden_dim: int = 4096):
        super().__init__()
        # QKV fusionne 12288=3×4096, proj 4096, mlp gate/up 24576 -> down 12288->4096
        # Shapes CONFIRMÉ par outputs/tensor_inventory.txt
        self.img_attn = nn.ModuleDict(
            {
                "qkv": nn.Linear(hidden_dim, hidden_dim * 3, bias=False),
                "proj": nn.Linear(hidden_dim, hidden_dim, bias=False),
                "norm": nn.ModuleDict(
                    {
                        "key_norm": nn.RMSNorm(128, eps=EPS),
                        "query_norm": nn.RMSNorm(128, eps=EPS),
                    }
                ),
            }
        )
        self.txt_attn = nn.ModuleDict(
            {
                "qkv": nn.Linear(hidden_dim, hidden_dim * 3, bias=False),
                "proj": nn.Linear(hidden_dim, hidden_dim, bias=False),
                "norm": nn.ModuleDict(
                    {
                        "key_norm": nn.RMSNorm(128, eps=EPS),
                        "query_norm": nn.RMSNorm(128, eps=EPS),
                    }
                ),
            }
        )
        # SwiGLU: linear_in 2×inner, linear_out
        self.img_mlp = nn.ModuleDict(
            {
                "0": nn.Linear(hidden_dim, 24576, bias=False),  # gate+up
                "2": nn.Linear(12288, hidden_dim, bias=False),
            }
        )
        self.txt_mlp = nn.ModuleDict(
            {
                "0": nn.Linear(hidden_dim, 24576, bias=False),  # gate+up
                "2": nn.Linear(12288, hidden_dim, bias=False),
            }
        )
        self.img_norm1 = nn.LayerNorm(hidden_dim, elementwise_affine=False, eps=EPS)
        self.txt_norm1 = nn.LayerNorm(hidden_dim, elementwise_affine=False, eps=EPS)
        self.img_norm2 = nn.LayerNorm(hidden_dim, elementwise_affine=False, eps=EPS)
        self.txt_norm2 = nn.LayerNorm(hidden_dim, elementwise_affine=False, eps=EPS)

    def forward(
        self, img, txt, vec_img, vec_txt, rope_cos, rope_sin, kv_cache=None, ref_len=0
    ):
        # Attention JOINT + split KV: refs only self-attend.
        B = img.shape[0]
        Li = img.shape[1]  # img sequence length (ref+target when refs present)
        Lt = txt.shape[1]
        # Normalize modulation to support (B,1,D*6) and per-token (B,S,D*6).
        if vec_img.ndim == 2:
            vec_img = vec_img.unsqueeze(1)
        if vec_txt.ndim == 2:
            vec_txt = vec_txt.unsqueeze(1)
        img_mod = vec_img.chunk(6, dim=-1)
        txt_mod = vec_txt.chunk(6, dim=-1)

        h = self.img_norm1(img)
        h = (1 + img_mod[1]) * h + img_mod[0]
        q, k, v = self.img_attn["qkv"](h).reshape(B, Li, 3, N_HEADS, HEAD_DIM).unbind(2)
        h2 = self.txt_norm1(txt)
        h2 = (1 + txt_mod[1]) * h2 + txt_mod[0]
        q2, k2, v2 = (
            self.txt_attn["qkv"](h2).reshape(B, Lt, 3, N_HEADS, HEAD_DIM).unbind(2)
        )

        img_norm = cast(nn.ModuleDict, self.img_attn["norm"])
        txt_norm = cast(nn.ModuleDict, self.txt_attn["norm"])
        q = cast(nn.RMSNorm, img_norm["query_norm"])(q)
        k = cast(nn.RMSNorm, img_norm["key_norm"])(k)
        q2 = cast(nn.RMSNorm, txt_norm["query_norm"])(q2)
        k2 = cast(nn.RMSNorm, txt_norm["key_norm"])(k2)

        q_txt, k_txt, v_txt = q2, k2, v2
        q_img, k_img, v_img = q, k, v

        # Fallback for cached KV: keep original full concat attention.
        if kv_cache is not None:
            qj = torch.cat([q_txt, q_img], dim=1)
            kj = torch.cat([k_txt, k_img], dim=1)
            vj = torch.cat([v_txt, v_img], dim=1)
            qj = apply_rope_interleaved(qj, rope_cos, rope_sin)
            kj = apply_rope_interleaved(kj, rope_cos, rope_sin)
            k_ref, v_ref = kv_cache.get()
            if k_ref.dtype == torch.float8_e4m3fn:
                k_ref = k_ref.to(torch.bfloat16)
            if v_ref.dtype == torch.float8_e4m3fn:
                v_ref = v_ref.to(torch.bfloat16)
            kj = torch.cat([kj, k_ref], dim=1)
            vj = torch.cat([vj, v_ref], dim=1)
            attn = F.scaled_dot_product_attention(
                qj.transpose(1, 2), kj.transpose(1, 2), vj.transpose(1, 2)
            ).transpose(1, 2)
            attn_t, attn_i = attn.split([Lt, Li], dim=1)
            img = img + img_mod[2] * self.img_attn["proj"](attn_i.reshape(B, Li, -1))
            txt = txt + txt_mod[2] * self.txt_attn["proj"](attn_t.reshape(B, Lt, -1))
            # MLP
            h3 = self.img_norm2(img)
            h3 = (1 + img_mod[4]) * h3 + img_mod[3]
            gate, up = self.img_mlp["0"](h3).chunk(2, dim=-1)
            img = img + img_mod[5] * self.img_mlp["2"](F.silu(gate) * up)
            h4 = self.txt_norm2(txt)
            h4 = (1 + txt_mod[4]) * h4 + txt_mod[3]
            gate2, up2 = self.txt_mlp["0"](h4).chunk(2, dim=-1)
            txt = txt + txt_mod[5] * self.txt_mlp["2"](F.silu(gate2) * up2)
            return img, txt

        R = int(ref_len)
        if R <= 0:
            # Standard full attention (T2I).
            qj = torch.cat([q_txt, q_img], dim=1)
            kj = torch.cat([k_txt, k_img], dim=1)
            vj = torch.cat([v_txt, v_img], dim=1)
            qj = apply_rope_interleaved(qj, rope_cos, rope_sin)
            kj = apply_rope_interleaved(kj, rope_cos, rope_sin)
            attn = F.scaled_dot_product_attention(
                qj.transpose(1, 2), kj.transpose(1, 2), vj.transpose(1, 2)
            ).transpose(1, 2)
            attn_t, attn_i = attn.split([Lt, Li], dim=1)
            img = img + img_mod[2] * self.img_attn["proj"](attn_i.reshape(B, Li, -1))
            txt = txt + txt_mod[2] * self.txt_attn["proj"](attn_t.reshape(B, Lt, -1))
            # MLP
            h3 = self.img_norm2(img)
            h3 = (1 + img_mod[4]) * h3 + img_mod[3]
            gate, up = self.img_mlp["0"](h3).chunk(2, dim=-1)
            img = img + img_mod[5] * self.img_mlp["2"](F.silu(gate) * up)
            h4 = self.txt_norm2(txt)
            h4 = (1 + txt_mod[4]) * h4 + txt_mod[3]
            gate2, up2 = self.txt_mlp["0"](h4).chunk(2, dim=-1)
            txt = txt + txt_mod[5] * self.txt_mlp["2"](F.silu(gate2) * up2)
            return img, txt

        # Extract-style split: refs [Lt:Lt+R] only self-attend; txt+target attend all.
        # Layout within img sequence: [ref(R), target(Tg)].
        q_ref = q_img[:, :R]
        q_target = q_img[:, R:]
        k_ref = k_img[:, :R]
        k_target = k_img[:, R:]
        v_ref = v_img[:, :R]
        v_target = v_img[:, R:]

        # txt + target attend to all tokens.
        q_other = torch.cat([q_txt, q_target], dim=1)
        k_all = torch.cat([k_txt, k_img], dim=1)
        v_all = torch.cat([v_txt, v_img], dim=1)
        q_txt_r = apply_rope_interleaved(q_txt, rope_cos[:Lt], rope_sin[:Lt])
        q_target_r = apply_rope_interleaved(
            q_target, rope_cos[Lt + R :], rope_sin[Lt + R :]
        )
        q_other_r = torch.cat([q_txt_r, q_target_r], dim=1)
        k_all_r = apply_rope_interleaved(
            k_all, rope_cos[: Lt + Li], rope_sin[: Lt + Li]
        )
        y_other = F.scaled_dot_product_attention(
            q_other_r.transpose(1, 2), k_all_r.transpose(1, 2), v_all.transpose(1, 2)
        ).transpose(1, 2)

        # refs self-attend only.
        q_ref_r = apply_rope_interleaved(
            q_ref, rope_cos[Lt : Lt + R], rope_sin[Lt : Lt + R]
        )
        k_ref_r = apply_rope_interleaved(
            k_ref, rope_cos[Lt : Lt + R], rope_sin[Lt : Lt + R]
        )
        y_ref = F.scaled_dot_product_attention(
            q_ref_r.transpose(1, 2), k_ref_r.transpose(1, 2), v_ref.transpose(1, 2)
        ).transpose(1, 2)

        y_txt = y_other[:, :Lt]
        y_target = y_other[:, Lt:]
        attn = torch.cat([y_txt, y_ref, y_target], dim=1)
        attn_t = attn[:, :Lt]
        attn_i = attn[:, Lt:]

        img = img + img_mod[2] * self.img_attn["proj"](attn_i.reshape(B, Li, -1))
        txt = txt + txt_mod[2] * self.txt_attn["proj"](attn_t.reshape(B, Lt, -1))

        # MLP SwiGLU
        h3 = self.img_norm2(img)
        h3 = (1 + img_mod[4]) * h3 + img_mod[3]
        gate, up = self.img_mlp["0"](h3).chunk(2, dim=-1)
        img = img + img_mod[5] * self.img_mlp["2"](F.silu(gate) * up)

        h4 = self.txt_norm2(txt)
        h4 = (1 + txt_mod[4]) * h4 + txt_mod[3]
        gate2, up2 = self.txt_mlp["0"](h4).chunk(2, dim=-1)
        txt = txt + txt_mod[5] * self.txt_mlp["2"](F.silu(gate2) * up2)

        return img, txt


class Flux2KleinSingleBlock(nn.Module):
    """Single block natif (4 tenseurs) — parallel QKV+MLP (linear1 36864, linear2 16384)."""

    def __init__(self, hidden_dim: int = 4096):
        super().__init__()
        self.norm = nn.LayerNorm(hidden_dim, elementwise_affine=False, eps=EPS)
        # Parallel: qkv 12288 + mlp 24576 = 36864 (flux2)
        self.linear1: nn.Module = nn.Linear(hidden_dim, 36864, bias=False)
        self.linear2: nn.Module = nn.Linear(16384, hidden_dim, bias=False)
        self.qk_norm = nn.ModuleDict(
            {
                "query_norm": nn.RMSNorm(128, eps=EPS),
                "key_norm": nn.RMSNorm(128, eps=EPS),
            }
        )

    def forward(
        self, x, vec, rope_cos, rope_sin, kv_cache=None, ref_len=0, txt_len=None
    ):
        # Normalize modulation.
        if vec.ndim == 2:
            vec = vec.unsqueeze(1)
        shift, scale, gate = vec.chunk(3, dim=-1)
        B = x.shape[0]
        S = x.shape[1]
        H = N_HEADS
        D = HEAD_DIM
        R = int(ref_len)
        T = int(txt_len) if txt_len is not None else 0
        h = self.norm(x)
        h = (1 + scale) * h + shift
        proj = self.linear1(h)
        qkv, mlp = proj.split([12288, 24576], dim=-1)
        q, k, v = qkv.reshape(B, S, 3, H, D).unbind(2)
        q = self.qk_norm["query_norm"](q)
        k = self.qk_norm["key_norm"](k)

        if kv_cache is not None:
            # Cached fallback: original concat (best effort).
            k_ref, v_ref = kv_cache.get()
            if k_ref.dtype == torch.float8_e4m3fn:
                k_ref = k_ref.to(torch.bfloat16)
            if v_ref.dtype == torch.float8_e4m3fn:
                v_ref = v_ref.to(torch.bfloat16)
            k = torch.cat([k, k_ref], dim=1)
            v = torch.cat([v, v_ref], dim=1)
        if R > 0 and kv_cache is None and T + R <= S:
            # Layout [txt T, ref R, target I]. refs only self-attend.
            q_ref = q[:, T : T + R]
            q_txt = q[:, :T]
            q_target = q[:, T + R :]
            k_ref = k[:, T : T + R]
            k_target = k[:, T + R :]
            v_ref = v[:, T : T + R]
            v_target = v[:, T + R :]
            # txt+target attend all.
            q_other_parts = []
            if T > 0:
                q_other_parts.append(
                    apply_rope_interleaved(q_txt, rope_cos[:T], rope_sin[:T])
                )
            if S - (T + R) > 0:
                q_other_parts.append(
                    apply_rope_interleaved(
                        q_target, rope_cos[T + R :], rope_sin[T + R :]
                    )
                )
            if len(q_other_parts) > 0:
                q_other = torch.cat(q_other_parts, dim=1)
            else:
                q_other = q_ref.new_empty(B, 0, H, D)
            k_all = k  # k includes txt/ref/target already in order positions
            # rope for k all:
            k_all_r = apply_rope_interleaved(k_all, rope_cos[:S], rope_sin[:S])
            v_all = v
            if q_other.shape[1] > 0:
                y_other = F.scaled_dot_product_attention(
                    q_other.transpose(1, 2),
                    k_all_r.transpose(1, 2),
                    v_all.transpose(1, 2),
                ).transpose(1, 2)
            else:
                y_other = None
            q_ref_r = apply_rope_interleaved(
                q_ref, rope_cos[T : T + R], rope_sin[T : T + R]
            )
            k_ref_r = apply_rope_interleaved(
                k_ref, rope_cos[T : T + R], rope_sin[T : T + R]
            )
            y_ref = F.scaled_dot_product_attention(
                q_ref_r.transpose(1, 2), k_ref_r.transpose(1, 2), v_ref.transpose(1, 2)
            ).transpose(1, 2)
            # reconstruct
            y_chunks = []
            if T > 0:
                y_chunks.append(
                    y_other[:, :T]
                    if y_other is not None
                    else q_txt.new_empty(B, 0, H, D)
                )
            y_chunks.append(y_ref)
            if S - (T + R) > 0:
                # target outputs are after txt outputs in y_other
                offset = T if T > 0 else 0
                y_chunks.append(
                    y_other[:, offset:]
                    if y_other is not None
                    else q_target.new_empty(B, 0, H, D)
                )
            attn = torch.cat(y_chunks, dim=1)
        else:
            if kv_cache is None:
                q_r = apply_rope_interleaved(q, rope_cos[:S], rope_sin[:S])
                k_r = apply_rope_interleaved(k, rope_cos[:S], rope_sin[:S])
                attn = F.scaled_dot_product_attention(
                    q_r.transpose(1, 2), k_r.transpose(1, 2), v.transpose(1, 2)
                ).transpose(1, 2)
            else:
                q_r = apply_rope_interleaved(q, rope_cos[:S], rope_sin[:S])
                k_r = apply_rope_interleaved(
                    k, rope_cos[:S], rope_sin[:S]
                )  # best effort
                attn = F.scaled_dot_product_attention(
                    q_r.transpose(1, 2), k_r.transpose(1, 2), v.transpose(1, 2)
                ).transpose(1, 2)
        attn = attn.reshape(B, S, H * D)
        gate_mlp, up = mlp.chunk(2, dim=-1)
        mlp_out = F.silu(gate_mlp) * up
        combined = torch.cat([attn, mlp_out], dim=-1)
        out = self.linear2(combined)
        return x + gate * out


class Flux2KleinTransformerHardcode(nn.Module):
    """
    Transformer hardcode from scratch (v3 §2.5).
    8 double_blocks + 24 single_blocks, noms natifs GGUF, lazy dequant par bloc.
    """

    def __init__(self, config, lazy=None, device="cuda", dtype=torch.bfloat16):
        super().__init__()
        self.config = config
        hidden = config.hidden_dim
        # Si lazy fourni, on branchera les poids GGUF par bloc (pas d'alloc upfront)
        self.lazy = lazy
        self.device = device
        self.dtype = dtype

        # Input projections — noms natifs
        self.img_in = nn.Linear(
            config.in_channels * config.patch_size**2, hidden, bias=False
        )
        self.txt_in = nn.Linear(12288, hidden, bias=False)  # taps concat
        self.time_in = nn.ModuleDict(
            {
                "in_layer": nn.Linear(256, hidden, bias=False),
                "out_layer": nn.Linear(hidden, hidden, bias=False),
            }
        )
        # Modulations — 6×hidden pour double, 3× pour single (issue #13001)
        self.double_stream_modulation_img = nn.ModuleDict(
            {"linear": nn.Linear(hidden, hidden * 6, bias=False)}
        )
        self.double_stream_modulation_txt = nn.ModuleDict(
            {"linear": nn.Linear(hidden, hidden * 6, bias=False)}
        )
        self.single_stream_modulation = nn.ModuleDict(
            {"lin": nn.Linear(hidden, hidden * 3, bias=False)}
        )

        # Blocks
        n_double = getattr(config, "num_double_blocks", 8)
        n_single = getattr(config, "num_single_blocks", 24)
        self.double_blocks = nn.ModuleList(
            [Flux2KleinDoubleBlock(hidden) for _ in range(n_double)]
        )
        self.single_blocks = nn.ModuleList(
            [Flux2KleinSingleBlock(hidden) for _ in range(n_single)]
        )

        # Output — AdaLayerNormContinue : norm + scale/shift de SiLU(temb)
        # (diffusers norm_out(temb) ; GGUF : final_layer.adaLN_modulation.1.weight)
        self.norm_out = nn.LayerNorm(hidden, elementwise_affine=False, eps=EPS)
        self.final_adaLN = nn.Linear(hidden, hidden * 2, bias=False)
        self.proj_out = nn.Linear(
            hidden,
            config.patch_size * config.patch_size * config.out_channels,
            bias=False,
        )

        # KV cache — double + single separes (diffusers Flux2KVCache)
        self.kv_cache = None

        # Compat ancien code (sampler itere blocks)
        self.blocks = self.double_blocks

    def forward(
        self, latents, t, ctx_12288, ref_kv=None, kv_cache_mode=None, ref_latents=None
    ):
        # latents: [B,32,H/8,W/8], t: [B] échelle [0,1000],
        # ctx_12288: [B,S,12288]
        B, C, H, W = latents.shape
        # Patchify 2×2 (equivaut au _patchify_latents klein : 32ch -> 128ch, grille /2)
        p = self.config.patch_size
        x = (
            latents.reshape(B, C, H // p, p, W // p, p)
            .permute(0, 2, 4, 1, 3, 5)
            .reshape(B, -1, C * p * p)
        )
        Li = x.shape[1]
        x = self.img_in(x)
        txt = self.txt_in(ctx_12288)
        Lt = txt.shape[1]

        # RoPE joint [txt;img] : txt (0,0,0,l), img (0,h,w,0) row-major
        dev = x.device
        txt_ids = torch.cartesian_prod(
            torch.zeros(1, dtype=torch.long, device=dev),
            torch.zeros(1, dtype=torch.long, device=dev),
            torch.zeros(1, dtype=torch.long, device=dev),
            torch.arange(Lt, device=dev),
        )
        Hp, Wp = H // p, W // p
        img_ids = torch.cartesian_prod(
            torch.zeros(1, dtype=torch.long, device=dev),
            torch.arange(Hp, device=dev),
            torch.arange(Wp, device=dev),
            torch.zeros(1, dtype=torch.long, device=dev),
        )

        ref_len = 0
        ref_ids_parts = []
        ref_x_parts = []
        if ref_latents is not None:
            ref_list = (
                list(ref_latents)
                if isinstance(ref_latents, (list, tuple))
                else [ref_latents]
            )
            for index, one_ref in enumerate(ref_list):
                _Br, Cr, Hr, Wr = one_ref.shape
                ref_flat = (
                    one_ref.reshape(B, Cr, Hr // p, p, Wr // p, p)
                    .permute(0, 2, 4, 1, 3, 5)
                    .reshape(B, -1, Cr * p * p)
                )
                ref_x = self.img_in(ref_flat)
                ref_len += int(ref_x.shape[1])
                ref_x_parts.append(ref_x)
                ref_ids_part = torch.cartesian_prod(
                    torch.tensor([10 * (index + 1)], dtype=torch.long, device=dev),
                    torch.arange(Hr // p, device=dev),
                    torch.arange(Wr // p, device=dev),
                    torch.zeros(1, dtype=torch.long, device=dev),
                )
                ref_ids_parts.append(ref_ids_part)
        # Append refs BEFORE target.
        if ref_x_parts:
            x = torch.cat([torch.cat(ref_x_parts, dim=1), x], dim=1)
        if not ref_ids_parts:
            rope_ids = torch.cat([txt_ids, img_ids], dim=0)
        else:
            ref_ids = torch.cat(ref_ids_parts, dim=0)
            rope_ids = torch.cat([txt_ids, ref_ids, img_ids], dim=0)
        rope_cos, rope_sin = build_rope_joint(rope_ids)

        # Le caller fournit le timestep dans l'échelle 0..1000 du modèle.
        t_emb = self._timestep_embedding(t, dim=256)
        temb = F.silu(self.time_in["in_layer"](t_emb))
        temb = self.time_in["out_layer"](temb)
        # Modulations : Linear(SiLU(temb)) — cf. Flux2Modulation
        temb_s = F.silu(temb)
        mod_img = self.double_stream_modulation_img["linear"](temb_s)
        mod_txt = self.double_stream_modulation_txt["linear"](temb_s)
        mod_single = self.single_stream_modulation["lin"](temb_s)

        # Reference modulation: fixed timestep (0) for ref tokens, blend into layout.
        if ref_len > 0:
            t0 = torch.zeros_like(t)
            t_emb0 = self._timestep_embedding(t0, dim=256)
            temb0 = F.silu(self.time_in["in_layer"](t_emb0))
            temb0 = self.time_in["out_layer"](temb0)
            temb_s0 = F.silu(temb0)
            mod_img_ref = self.double_stream_modulation_img["linear"](temb_s0)
            mod_single_ref = self.single_stream_modulation["lin"](temb_s0)
            # Double layout image stream: [ref, target].
            mod_img = torch.cat(
                [
                    mod_img_ref.unsqueeze(1).expand(-1, ref_len, -1),
                    mod_img.unsqueeze(1).expand(-1, Li, -1),
                ],
                dim=1,
            )
            # Single stream: [txt, ref, target].
            mod_single = torch.cat(
                [
                    mod_single.unsqueeze(1).expand(-1, Lt, -1),
                    mod_single_ref.unsqueeze(1).expand(-1, ref_len, -1),
                    mod_single.unsqueeze(1).expand(-1, Li, -1),
                ],
                dim=1,
            )

        # Double blocks (attention joint)
        for i, blk in enumerate(self.double_blocks):
            cache = (
                self.kv_cache.double_block_caches[i]
                if self.kv_cache and kv_cache_mode == "cached"
                else None
            )
            x, txt = blk(
                x,
                txt,
                mod_img,
                mod_txt,
                rope_cos,
                rope_sin,
                kv_cache=cache,
                ref_len=ref_len,
            )
            if kv_cache_mode == "extract":
                # Stocker K/V ref (simplifie)
                pass

        # Single blocks — concat txt+img pour single stream
        if len(self.single_blocks) > 0:
            # Flux2 single: txt et img concatene
            h = torch.cat([txt, x], dim=1)
            for i, blk in enumerate(self.single_blocks):
                cache = (
                    self.kv_cache.single_block_caches[i]
                    if self.kv_cache and kv_cache_mode == "cached"
                    else None
                )
                h = blk(
                    h,
                    mod_single,
                    rope_cos,
                    rope_sin,
                    kv_cache=cache,
                    ref_len=ref_len,
                    txt_len=Lt,
                )
            # Split
            txt, x = h[:, :Lt], h[:, Lt:]

        # Sortie : on ignore les tokens de condition source ajoutés.
        ada = self.final_adaLN(temb_s)
        scale, shift = ada.chunk(2, dim=-1)
        x = x[:, ref_len:]
        x = self.norm_out(x) * (1 + scale).unsqueeze(1) + shift.unsqueeze(1)
        x = self.proj_out(x)
        # Unpatchify
        H_p, W_p = H // p, W // p
        x = (
            x.reshape(B, H_p, W_p, self.config.out_channels, p, p)
            .permute(0, 3, 1, 4, 2, 5)
            .reshape(B, self.config.out_channels, H, W)
        )
        return x

    @staticmethod
    def _timestep_embedding(timesteps, dim=256, max_period=10000):
        if timesteps.dim() == 0:
            timesteps = timesteps.unsqueeze(0)
        half = dim // 2
        freqs = torch.exp(
            -torch.log(torch.tensor(max_period, dtype=torch.float32))
            * torch.arange(half, dtype=torch.float32)
            / half
        ).to(timesteps.device)
        args = timesteps.float().unsqueeze(1) * freqs.unsqueeze(0)
        emb = torch.cat([torch.cos(args), torch.sin(args)], dim=-1)
        if dim % 2 == 1:
            emb = torch.cat([emb, torch.zeros_like(emb[:, :1])], dim=-1)
        return emb.to(
            timesteps.dtype
            if timesteps.dtype in (torch.float32, torch.bfloat16, torch.float16)
            else torch.float32
        )

    def clear_kv_cache(self):
        if self.kv_cache:
            self.kv_cache.clear()
        self.kv_cache = None


class LazyLinear(nn.Module):
    """LazyLinear — raw VRAM + dequant GPU on-the-fly (rapport_lazy-dequant §3).

    ET POURQUOI CE `forward` REND LES PAGES DU FICHIER, À LA FIN ET PAS AILLEURS
    ============================================================================
    `RawVRAMIndex` sert des VUES sur le fichier de poids (5,32 Go), sans les copier.
    Une vue lue devient une page RÉSIDENTE, donc facturée en `RssFile` — et
    `RssFile` est sa propre porte (« RssFile ~baseline », non additive) : elle ne
    demande pas « le cumul tient-il ? » mais « paie-t-on le fichier qu'on peut
    évicter ? ». Or il s'évince : après la déquantisation d'un tenseur, ses octets
    quantifiés ne sont plus relus dans CE forward, et le `forward` suivant du même
    tenseur — c'est-à-dire le pas de diffusion suivant — les re-demandera au
    noyau, depuis le cache de pages s'il l'a encore, depuis le disque sinon.

    L'APPEL EST ICI, ET N'ICI NULLE PART AILLEURS, POUR TROIS RAISONS MESURÉES :

    * APRÈS `F.linear`, jamais avant. `F.linear` est asynchrone côté CUDA : Relecher
      les pages entre la déquantisation et la multiplication laisserait un
      `cudaMemcpyAsync` lire un mapping que le noyau a déjà vidé. L'octet
      quantifié est recopié en VRAM par `raw.to(x.device)` — et, même en
      `non_blocking`, le pilote a recopié la source pageable dans son tampon
      d'étage AVANT de rendre la main (comportement documenté de
      `cudaMemcpyAsync`), donc à la sortie de cette ligne les octets du fichier
      ne sont plus en vol : les relâcher n'est plus qu'un geste sur du cache ;
    * APRÈS `del w` : le poids déquantisé ne partage plus rien avec le mapping, et
      rien d'autre ne le retient ;
    * À CHAQUE FORWARD, ET PAS SEULEMENT AU PREMIER. `MADV_DONTNEED` porte sur
      TOUTE la mapping, mais ne retire que les pages DÉJÀ peuplées. Un appel unique
      au début de la diffusion ne lâcherait que le premier tenseur lu, et le
      fichier se reimbriplerait jusqu'à 5,32 Go avant la fin du run — c'est-à-dire
      jusqu'à l'instant exact où la porte est jugée.

    CE QUE ÇA COÛTE : un re-fault par tenseur et par pas de diffusion. Ce que ça
    gagne : le processus cesse de se facturer un fichier qu'il ne fait que lire.
    Les chiffres sont dans `RawVRAMIndex.relacher_les_pages`, et le résultat, lui,
    est le même bit pour bit : `MADV_DONTNEED` ne démonte rien et n'efface rien.
    """

    def __init__(self, index, name, bias=False):
        super().__init__()
        self.index = index
        self.name = name
        self.bias_flag = bias

    def forward(self, x):
        from ..loaders.dequant_gpu import dequantize_gpu

        shape, qname = self.index.meta[self.name]
        raw = self.index.raw[self.name]
        if raw.device != x.device:
            raw = raw.to(x.device, non_blocking=True)
        import gguf

        qtype = getattr(gguf.GGMLQuantizationType, qname)
        w = dequantize_gpu(raw, qtype, shape, dtype=torch.bfloat16)
        # Handle transpose for 2D (GGUF stores transposed)
        if w.dim() == 2 and w.shape != torch.Size(shape):
            # dequant already handled shape, but GGUF stores [out,in] vs [in,out]
            pass
        y = F.linear(x, w)
        del w
        # Le point de relâchement, et le seul : voir l'en-tête de la classe.
        self.index.relacher_les_pages()
        return y


def build_transformer_lazy(index, config=None, device="cuda", dtype=torch.bfloat16):
    """Construit le transformer hardcode et lie les 201 tenseurs natifs GGUF — strict on-device, zero cache (v2 §2)."""
    from ..core.config import FLUX2_KLEIN_CONFIG
    import gc

    cfg = config or FLUX2_KLEIN_CONFIG
    tr = Flux2KleinTransformerHardcode(cfg, device=device, dtype=dtype)
    bound = []

    # Remplacement reel des nn.Linear par LazyLinear (pas de double alloc)
    # On parcourt tous les Linear du transformer et on les remplace si le GGUF a la cle
    for module_name, module in list(tr.named_modules()):
        if isinstance(module, nn.Linear):
            # module_name est comme "double_blocks.0.img_attn.qkv" ou "time_in.in_layer"
            gguf_name = f"{module_name}.weight"
            # Variations pour ModuleDict avec "0"/"2" et "linear"/"lin"
            candidates = [
                gguf_name,
                gguf_name.replace(".0.weight", ".weight"),
                gguf_name.replace(".2.weight", ".weight"),
                gguf_name.replace(
                    "double_stream_modulation_img.linear",
                    "double_stream_modulation_img.lin",
                ),
                gguf_name.replace(
                    "double_stream_modulation_txt.linear",
                    "double_stream_modulation_txt.lin",
                ),
                gguf_name.replace(
                    "single_stream_modulation.lin",
                    "single_stream_modulation.lin.weight",
                ),
                gguf_name.replace("img_in.weight", "img_in.weight"),
                gguf_name.replace("proj_out.weight", "proj_out.weight"),
                gguf_name.replace("txt_in.weight", "txt_in.weight"),
                # Couches de sortie : noms code vs noms GGUF natifs
                gguf_name.replace(
                    "final_adaLN.weight", "final_layer.adaLN_modulation.1.weight"
                ),
                gguf_name.replace("proj_out.weight", "final_layer.linear.weight"),
            ]
            for cand in candidates:
                if cand in index.raw:
                    # Trouver le parent et remplacer
                    parts = module_name.split(".")
                    parent = tr
                    for p in parts[:-1]:
                        # Handle ModuleList indices
                        if p.isdigit():
                            parent = cast(nn.ModuleList, parent)[int(p)]
                        else:
                            # ModuleDict or attribute
                            if hasattr(parent, p):
                                parent = getattr(parent, p)
                            elif isinstance(parent, nn.ModuleDict) and p in parent:
                                parent = cast(nn.ModuleDict, parent)[p]
                            elif isinstance(parent, nn.ModuleList):
                                parent = cast(nn.ModuleList, parent)[int(p)]
                            else:
                                parent = None
                                break
                    if parent is not None:
                        leaf = parts[-1]
                        try:
                            # Detruire l'ancien Linear (libere ses poids random 6.53B)
                            old = (
                                getattr(parent, leaf)
                                if hasattr(parent, leaf)
                                else parent[leaf]
                                if isinstance(parent, nn.ModuleDict)
                                else None
                            )
                            if old is not None:
                                del old
                            if isinstance(parent, nn.ModuleDict):
                                parent[leaf] = LazyLinear(index, cand)
                            else:
                                setattr(parent, leaf, LazyLinear(index, cand))
                            bound.append(cand)
                            break
                        except Exception:
                            pass

    # Gate strict : ne comptent que les Linear VRAIMENT remplacés. Tout Linear restant
    # garde des poids random → bruit en sortie (cas final_adaLN/proj_out, sept 2026).
    n_linear = sum(1 for _, m in tr.named_modules() if isinstance(m, nn.Linear))
    bound = list(dict.fromkeys(bound))
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    assert len(bound) == n_linear, (
        f"binding incomplet: {len(bound)}/{n_linear} Linear liés"
    )
    return tr, bound


# Alias compat
Flux2KleinTransformer = Flux2KleinTransformerHardcode
# Garde les anciens blocks pour orchestrator qui importe Flux2KleinBlock
Flux2KleinBlock = Flux2KleinDoubleBlock
