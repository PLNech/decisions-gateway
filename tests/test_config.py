import pytest

from decisions_gateway.config import (
    DEFAULT_KEY_FILE,
    DEFAULT_KEY_VAR,
    KEY_FILE_ENV,
    Backend,
    Config,
    ConfigError,
    load_config,
    read_key,
)


@pytest.fixture(autouse=True)
def no_env_override(monkeypatch):
    monkeypatch.delenv(KEY_FILE_ENV, raising=False)


def test_defaults_when_default_file_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = load_config()
    assert cfg.listen_host == "127.0.0.1" and cfg.listen_port == 8017
    assert cfg.default_backend == "default"
    assert cfg.backends == {"default": Backend("default", "127.0.0.1", 8117)}
    assert cfg.max_questions == 32 and cfg.max_options == 26 and cfg.max_budget == 256
    assert cfg.key_file == DEFAULT_KEY_FILE == "~/.config/decisions-gateway/key.env"
    assert cfg.key_var == DEFAULT_KEY_VAR == "DECISIONS_GATEWAY_API_KEY"
    assert cfg.known_clients == [] and cfg.known_models == []


def test_explicit_missing_file_is_an_error(tmp_path):
    with pytest.raises(ConfigError):
        load_config(str(tmp_path / "absent.toml"))


def test_toml_overrides(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        'listen_port = 18017\nmax_budget = 64\ndefault_backend = "b"\n'
        '[backends.b]\nhost = "localhost"\nport = 9999\nkey_var = "K"\n'
    )
    cfg = load_config(str(p))
    assert cfg.listen_port == 18017 and cfg.max_budget == 64
    b = cfg.backends["b"]
    assert (b.host, b.port, b.key_file, b.key_var) == ("localhost", 9999, None, "K")
    assert cfg.backend_key_source(b) == (DEFAULT_KEY_FILE, "K")


def test_backend_inherits_gateway_key_unless_set(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        'key_file = "/k/gw.env"\nkey_var = "GW"\n'
        "[backends.default]\nport = 8117\n"
        '[backends.own]\nport = 8118\nkey_file = "/k/own.env"\nkey_var = "OWN"\n'
    )
    cfg = load_config(str(p))
    assert cfg.backends["default"].host == "127.0.0.1"
    assert cfg.backend_key_source(cfg.backends["default"]) == ("/k/gw.env", "GW")
    assert cfg.backend_key_source(cfg.backends["own"]) == ("/k/own.env", "OWN")


def test_key_file_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(KEY_FILE_ENV, "/run/secrets/gw.env")
    cfg = load_config()
    assert cfg.key_file == "/run/secrets/gw.env"
    assert cfg.backend_key_source(cfg.backends["default"]) == ("/run/secrets/gw.env", DEFAULT_KEY_VAR)
    # the environment wins over the file
    p = tmp_path / "config.toml"
    p.write_text('key_file = "/from/toml.env"\n')
    assert load_config(str(p)).key_file == "/run/secrets/gw.env"
    # an empty variable is ignored
    monkeypatch.setenv(KEY_FILE_ENV, "")
    assert load_config(str(p)).key_file == "/from/toml.env"


def test_env_override_feeds_read_key(monkeypatch, tmp_path):
    key_file = tmp_path / "custom.env"
    key_file.write_text("DECISIONS_GATEWAY_API_KEY=dummy-key-0000\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(KEY_FILE_ENV, str(key_file))
    cfg = load_config()
    assert read_key(cfg.key_file, cfg.key_var) == "dummy-key-0000"


def test_label_allow_lists(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('known_clients = ["bench", "labeller"]\nknown_models = ["model-a"]\n')
    cfg = load_config(str(p))
    assert cfg.known_clients == ["bench", "labeller"] and cfg.known_models == ["model-a"]
    for bad in ('known_clients = "bench"\n', "known_clients = [{}]\n".format(", ".join(f'"c{i}"' for i in range(17)))):
        p.write_text(bad)
        with pytest.raises(ConfigError):
            load_config(str(p))


@pytest.mark.parametrize(
    "text",
    [
        "nope = 1\n",
        'max_questions = "many"\n',
        "[backends]\n",
        '[backends.default]\nurl = "http://127.0.0.1:8117"\n',
        '[backends.default]\nhost = "http://x"\nport = 1\n',
        "[backends.default]\nport = 0\n",
        '[backends.default]\nport = "8117"\n',
        "[backends.default]\nport = 8117\nkey_var = 1\n",
        'default_backend = "missing"\n',
        "[backends.other]\nport = 8117\n",
    ],
)
def test_bad_config_is_rejected(tmp_path, text):
    p = tmp_path / "c.toml"
    p.write_text(text)
    with pytest.raises(ConfigError):
        load_config(str(p))


def test_read_key_never_leaks_value(tmp_path):
    p = tmp_path / "k.env"
    p.write_text("OTHER=zzz\nexport DECISIONS_GATEWAY_API_KEY='s3cret-value'\n")
    assert read_key(str(p), "DECISIONS_GATEWAY_API_KEY") == "s3cret-value"
    with pytest.raises(ConfigError) as exc:
        read_key(str(p), "MISSING")
    assert "zzz" not in str(exc.value) and "s3cret" not in str(exc.value)
    p.write_text("DECISIONS_GATEWAY_API_KEY=\n")
    with pytest.raises(ConfigError):
        read_key(str(p), "DECISIONS_GATEWAY_API_KEY")
    with pytest.raises(ConfigError) as exc:
        read_key(str(tmp_path / "absent.env"), "DECISIONS_GATEWAY_API_KEY")
    assert "cannot read key file" in str(exc.value)


def test_config_dataclass_defaults_are_independent():
    a, b = Config(), Config()
    a.known_clients.append("x")
    assert b.known_clients == []
