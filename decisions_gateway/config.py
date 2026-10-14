"""Configuration: defaults in code, optional TOML overrides, key files read in-process."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

DEFAULT_CONFIG_PATH = "~/.config/decisions-gateway/config.toml"
DEFAULT_KEY_FILE = "~/.config/decisions-gateway/key.env"
DEFAULT_KEY_VAR = "DECISIONS_GATEWAY_API_KEY"
KEY_FILE_ENV = "DECISIONS_GATEWAY_KEY_FILE"  # overrides key_file from the TOML file
BACKEND_KEYS = {"host", "port", "key_file", "key_var"}


class ConfigError(Exception):
    """A configuration problem. Messages name files and variables, never values."""


@dataclass(frozen=True)
class Backend:
    name: str
    host: str
    port: int
    # None means: present the gateway's own key (key_file / key_var) to this backend.
    key_file: str | None = None
    key_var: str | None = None


@dataclass
class Config:
    listen_host: str = "127.0.0.1"
    listen_port: int = 8017
    default_backend: str = "default"
    log_level: str = "INFO"
    key_file: str = DEFAULT_KEY_FILE
    key_var: str = DEFAULT_KEY_VAR
    connect_timeout_s: float = 2.0
    read_timeout_s: float = 120.0
    max_body_bytes: int = 256 * 1024
    max_questions: int = 32
    max_options: int = 26
    max_budget: int = 256
    backend_max_in_flight: int = 8
    client_max_in_flight: int = 4
    retry_after_s: int = 1
    state_dir: str = "~/.local/state/decisions-gateway"
    flush_interval_s: float = 60.0
    ring_minutes: int = 7 * 24 * 60
    retention_days: int = 30
    max_rows: int = 500_000
    label_cap: int = 16  # max entries in each allow-list below
    # Label allow-lists: only these values (sanitized) become labels; anything else is "other".
    known_clients: list[str] = field(default_factory=list)
    known_models: list[str] = field(default_factory=list)
    backends: dict[str, Backend] = field(default_factory=lambda: {"default": Backend("default", "127.0.0.1", 8117)})

    def backend_key_source(self, backend: Backend) -> tuple[str, str]:
        """The (key file, variable) holding the key the gateway presents to `backend`."""
        return backend.key_file or self.key_file, backend.key_var or self.key_var


def parse_backend(name: str, raw: dict) -> Backend:
    if not isinstance(raw, dict):
        raise ConfigError(f"backend {name!r} must be a table")
    unknown = set(raw) - BACKEND_KEYS
    if unknown:
        raise ConfigError(f"backend {name!r}: unknown keys {sorted(unknown)}")
    host, port = raw.get("host", "127.0.0.1"), raw.get("port")
    if not isinstance(host, str) or not host or any(c in host for c in "/:@ "):
        raise ConfigError(f"backend {name!r}: host must be a hostname or IPv4 address")
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
        raise ConfigError(f"backend {name!r}: port must be an integer in 1..65535")
    for key in ("key_file", "key_var"):
        if key in raw and (not isinstance(raw[key], str) or not raw[key]):
            raise ConfigError(f"backend {name!r}: {key} must be a non-empty string")
    return Backend(name=name, host=host, port=port, key_file=raw.get("key_file"), key_var=raw.get("key_var"))


def load_config(path: str | None = None) -> Config:
    """Load the TOML file if it exists (a missing default file means all defaults), then apply env overrides."""
    cfg = Config()
    p = Path(os.path.expanduser(path or DEFAULT_CONFIG_PATH))
    if p.exists():
        with p.open("rb") as fh:
            apply_toml(cfg, tomllib.load(fh))
    elif path:
        raise ConfigError(f"config file not found: {p}")
    env_key_file = os.environ.get(KEY_FILE_ENV)
    if env_key_file:
        cfg.key_file = env_key_file
    for key in ("known_clients", "known_models"):
        if len(getattr(cfg, key)) > cfg.label_cap:
            raise ConfigError(f"{key} has more than label_cap={cfg.label_cap} entries")
    if cfg.default_backend not in cfg.backends:
        raise ConfigError(f"default_backend {cfg.default_backend!r} is not in [backends]")
    return cfg


def apply_toml(cfg: Config, raw: dict) -> None:
    known = {f.name for f in fields(Config)} - {"backends"}
    for key, value in raw.items():
        if key == "backends":
            if not isinstance(value, dict) or not value:
                raise ConfigError("[backends] must hold at least one [backends.<name>] table")
            cfg.backends = {name: parse_backend(name, b) for name, b in value.items()}
        elif key in ("known_clients", "known_models"):
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ConfigError(f"{key} must be a list of strings")
            setattr(cfg, key, value)
        elif key in known:
            try:
                setattr(cfg, key, type(getattr(cfg, key))(value))
            except (TypeError, ValueError):
                raise ConfigError(f"{key} has the wrong type") from None
        else:
            raise ConfigError(f"unknown config key: {key}")


def read_key(key_file: str, key_var: str) -> str:
    """Read KEY=value from an env file. Never returns or raises with the value in a message."""
    p = Path(os.path.expanduser(key_file))
    try:
        lines = p.read_text().splitlines()
    except OSError:
        raise ConfigError(f"cannot read key file {p}") from None
    for line in lines:
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        name, sep, value = line.partition("=")
        if sep and name.strip() == key_var:
            value = value.strip().strip("'\"")
            if value:
                return value
    raise ConfigError(f"{key_var} not set in {p}")
