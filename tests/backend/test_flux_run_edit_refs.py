def test_run_edit_forwards_reference_images(monkeypatch):
    from backend.services import flux_spike as fx

    captured = {}

    def fake_locked(*args, **kwargs):
        captured.update(kwargs)
        return b"png", {"seed": 7, "width": 512, "height": 512}

    monkeypatch.setattr(fx, "_run_t2i_locked", fake_locked)

    png, stats = fx.run_edit(
        "prompt",
        seed=7,
        init_image="/tmp/init.png",
        mode="global",
        reference_images=["/tmp/ref1.png", "/tmp/ref2.png"],
    )

    assert png == b"png"
    assert captured["reference_images"] == ["/tmp/ref1.png", "/tmp/ref2.png"]


def test_spike_cmd_emits_ref_image(tmp_path):
    from backend.services.flux_spike import _spike_cmd

    ref = tmp_path / "ref.png"
    ref.write_bytes(b"x")
    cmd, _cwd = _spike_cmd(
        "prompt.txt",
        7,
        512,
        512,
        "out.png",
        reference_images=[str(ref)],
    )
    assert "--ref-image" in cmd
    assert str(ref) in cmd
