from uma8_callmic.config import Config, load, save


def test_missing_file_gives_defaults(tmp_path):
    res = load(tmp_path / "config.toml")
    assert res.config == Config() and res.warnings == [] and not res.broken


def test_roundtrip(tmp_path):
    path = tmp_path / "config.toml"
    cfg = Config(active=False, direction_mode="tracking", calibrated=True, calibrated_azimuth=215.0,
                 gain_db=36.0, ring=[2, 3, 4, 5, 6, 1], dereverb=True)
    save(cfg, path)
    res = load(path)
    assert res.config == cfg and res.warnings == []


def test_invalid_values_fall_back_with_warning(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('gain_db = 500.0\ndirection_mode = "sideways"\nactive = "ja"\nmanual_azimuth = 12\n')
    res = load(path)
    assert res.config.gain_db == Config().gain_db
    assert res.config.direction_mode == "calibrated"
    assert res.config.active is True
    assert res.config.manual_azimuth == 12.0
    assert len(res.warnings) == 3


def test_invalid_ring_resets_geometry(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("center_channel = 1\nring = [1, 2, 3, 4, 5, 6]\n")
    res = load(path)
    assert res.config.center_channel == 0 and res.config.ring == [1, 6, 5, 4, 3, 2]
    assert any("Kanalzuordnung" in w for w in res.warnings)


def test_broken_file_is_backed_up_on_save(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("das ist [kein toml")
    res = load(path)
    assert res.broken and res.config == Config()
    save(res.config, path, broken=True)
    assert (tmp_path / "config.toml.broken").read_text() == "das ist [kein toml"
    assert load(path).config == Config()
