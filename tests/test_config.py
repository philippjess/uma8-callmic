from uma8_callmic.config import PROFILE_FIELDS, Config, load, profile_defaults, save, write_atomic


def test_missing_file_gives_defaults(tmp_path):
    res = load(tmp_path / "config.toml")
    assert res.config == Config() and res.warnings == [] and not res.broken


def test_roundtrip(tmp_path):
    path = tmp_path / "config.toml"
    cfg = Config(active=False, direction_mode="tracking", calibrated=True, calibrated_azimuth=215.0,
                 gain_db=36.0, ring=[2, 3, 4, 5, 6, 1], dereverb=False, late_reverb=False,
                 beamformer="delay_and_sum", echo_cancel=False)
    save(cfg, path)
    res = load(path)
    assert res.config == cfg and res.warnings == []


def test_invalid_values_fall_back_with_warning(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('gain_db = 500.0\ndirection_mode = "sideways"\nactive = "ja"\nmanual_azimuth = 12\n'
                    'beamformer = "omni"\necho_cancel = 1\n')
    res = load(path)
    assert res.config.gain_db == Config().gain_db
    assert res.config.direction_mode == "calibrated"
    assert res.config.active is True
    assert res.config.manual_azimuth == 12.0
    assert res.config.beamformer == "superdirective"
    assert res.config.echo_cancel is True
    assert len(res.warnings) == 5


def test_old_dereverb_switch_gives_new_defaults(tmp_path):
    """Vor der Kohärenz-Hallunterdrückung schaltete „dereverb“ das T60-Modell (jetzt „late_reverb“)."""
    path = tmp_path / "config.toml"
    path.write_text("dereverb = false\ndereverb_strength = 0.8\n")
    res = load(path)
    assert res.config.dereverb is False and res.config.late_reverb is True
    assert res.config.dereverb_strength == Config().dereverb_strength  # Bedeutung geändert → Standard
    assert res.migrated and res.warnings == []
    path.write_text("dereverb = false\nlate_reverb = false\ndereverb_strength = 0.8\n")
    res = load(path)
    assert res.config.dereverb is False and res.config.late_reverb is False
    assert res.config.dereverb_strength == 0.8 and not res.migrated


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


def test_echo_cancel_defaults_on_for_old_files(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("gain_db = 36.0\n")
    res = load(path)
    assert res.config.echo_cancel is True and res.config.gain_db == 36.0 and res.warnings == []
    save(Config(echo_cancel=False), path)
    assert "echo_cancel = false\n" in path.read_text()
    assert load(path).config.echo_cancel is False


def test_profile_roundtrip(tmp_path):
    path = tmp_path / "config.toml"
    cfg = Config(speakers=[[116.0, 8.0], [313.5, 10.0]], speaker_levels_dbfs=[-52.0, -54.5], keyboard=[213.0, 5.0],
                 null_weight_db=10.0, talker_zone_deg=40.0, noise_floor_dbfs=-77.0, speech_level_dbfs=-47.5)
    save(cfg, path)
    assert "speakers = [[116.0, 8.0], [313.5, 10.0]]\n" in path.read_text()
    res = load(path)
    assert res.config == cfg and res.warnings == [] and res.config.has_profile()


def test_no_profile_by_default_and_unset_levels_are_omitted(tmp_path):
    path = tmp_path / "config.toml"
    cfg = Config()
    assert not cfg.has_profile() and cfg.talker_zone_deg == 180.0 and cfg.speakers == []
    save(cfg, path)
    text = path.read_text()
    assert "noise_floor_dbfs" not in text and "speech_level_dbfs" not in text   # None: kein Schlüssel
    assert load(path).config == cfg
    assert profile_defaults() == {k: getattr(Config(), k) for k in PROFILE_FIELDS}


def test_invalid_profile_values_fall_back(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('speakers = [[100, 5], [200, 95]]\nkeyboard = [10]\nnull_weight_db = 50.0\n'
                    'talker_zone_deg = 2\nnoise_floor_dbfs = "leise"\nspeaker_levels_dbfs = [-50.0, true]\n')
    res = load(path)
    c = res.config
    assert c.speakers == [] and c.keyboard == [] and c.null_weight_db == 0.0 and c.talker_zone_deg == 180.0
    assert c.noise_floor_dbfs is None and c.speaker_levels_dbfs == [] and len(res.warnings) == 6
    path.write_text("speakers = [[100, 5], [200, 7], [300, 1]]\n")          # höchstens zwei
    assert load(path).config.speakers == []
    path.write_text("speakers = [[100, 5]]\nspeaker_levels_dbfs = [-50.0, -52.0]\n")
    res = load(path)
    assert res.config.speakers == [[100.0, 5.0]] and res.config.speaker_levels_dbfs == []
    assert any("Lautsprecherpegel" in w for w in res.warnings)


def test_write_atomic_replaces_whole_file(tmp_path):
    path = tmp_path / "neu" / "datei.conf"
    write_atomic(path, "eins\n")
    write_atomic(path, "zwei\n")
    assert path.read_text() == "zwei\n" and [p.name for p in path.parent.iterdir()] == ["datei.conf"]


def test_setup_flag_roundtrip_and_old_files(tmp_path):
    """Ältere Dateien kennen „setup_done“ nicht: einmalige Einrichtung steht dann noch aus."""
    path = tmp_path / "config.toml"
    path.write_text("gain_db = 30.0\nautostart = true\n")
    assert load(path).config.setup_done is False
    save(Config(setup_done=True), path)
    assert load(path).config.setup_done is True
