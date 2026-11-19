from backend.image_engine.spike_t2i import build_cond_refs


def test_t2i_no_mask_no_ref():
    assert build_cond_refs(
        mask_active=False,
        source_ref_latent="source",
        extra_refs=None,
        keep_source_reference=False,
    ) in (None, [])


def test_t2i_no_mask_with_refs_order():
    refs = ["r1", "r2"]
    out = build_cond_refs(
        mask_active=False,
        source_ref_latent="source",
        extra_refs=refs,
        keep_source_reference=False,
    )
    assert out == refs
    assert "source" not in (out or [])


def test_masked_no_source_preserves_refs():
    refs = ["r1", "r2"]
    out = build_cond_refs(
        mask_active=True,
        source_ref_latent=None,
        extra_refs=refs,
        keep_source_reference=True,
    )
    assert out == refs
    assert "source" not in (out or [])


def test_masked_source_then_refs():
    refs = ["r1", "r2"]
    out = build_cond_refs(
        mask_active=True,
        source_ref_latent="source",
        extra_refs=refs,
        keep_source_reference=True,
    )
    assert out == ["source", "r1", "r2"]


def test_source_absent_no_crash():
    refs = ["r1", "r2"]
    out = build_cond_refs(
        mask_active=True,
        source_ref_latent=None,
        extra_refs=refs,
        keep_source_reference=True,
    )
    assert out == refs
