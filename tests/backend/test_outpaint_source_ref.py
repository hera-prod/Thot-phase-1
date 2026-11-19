from backend.image_engine.source_ref import resolve_source_reference_mode


def test_outpaint_masked_default_original():
    assert resolve_source_reference_mode("outpaint", True, {}) == "original"


def test_inpaint_masked_none():
    assert resolve_source_reference_mode("inpaint", True, {}) == "none"


def test_local_masked_none():
    assert resolve_source_reference_mode("local", True, {}) == "none"


def test_env_none_override():
    assert (
        resolve_source_reference_mode(
            "outpaint", True, {"THOT_IMAGE_SOURCE_REFERENCE": "none"}
        )
        == "none"
    )


def test_env_original_override():
    assert (
        resolve_source_reference_mode(
            "outpaint", True, {"THOT_IMAGE_SOURCE_REFERENCE": "original"}
        )
        == "original"
    )


def test_env_work_override():
    assert (
        resolve_source_reference_mode(
            "outpaint", True, {"THOT_IMAGE_SOURCE_REFERENCE": "work"}
        )
        == "work"
    )


def test_no_mask_none():
    assert resolve_source_reference_mode("outpaint", False, {}) == "none"


def test_no_mask_env_still_none():
    assert (
        resolve_source_reference_mode(
            "outpaint", False, {"THOT_IMAGE_SOURCE_REFERENCE": "work"}
        )
        == "none"
    )


def test_env_no_flag():
    assert (
        resolve_source_reference_mode(
            "outpaint", True, {"THOT_IMAGE_NO_SOURCE_REFERENCE": "1"}
        )
        == "none"
    )


def test_env_keep_flag():
    assert (
        resolve_source_reference_mode(
            "outpaint", True, {"THOT_IMAGE_KEEP_SOURCE_REFERENCE": "1"}
        )
        == "work"
    )
