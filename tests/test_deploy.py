"""Deployment files stay in sync with the code."""

import re
from dataclasses import fields
from pathlib import Path

from decisions_gateway.config import BACKEND_KEYS, KEY_FILE_ENV, Config, load_config

DEPLOY = Path(__file__).parent.parent / "deploy"
EXAMPLE = DEPLOY / "config.example.toml"


def test_example_config_holds_the_defaults(monkeypatch):
    monkeypatch.delenv(KEY_FILE_ENV, raising=False)
    assert load_config(str(EXAMPLE)) == Config()


def test_example_config_documents_every_key():
    text = EXAMPLE.read_text()
    for name in {f.name for f in fields(Config)} - {"backends"} | BACKEND_KEYS:
        assert re.search(rf"^#? ?{name} = ", text, re.M), name


def test_unit_templates_use_only_known_placeholders():
    known = {"@USER@", "@GROUP@", "@HOME@", "@PREFIX@", "@BACKEND_DIR@", "@MODEL_DIR@"}
    for unit in (DEPLOY / "systemd").iterdir():
        found = set(re.findall(r"@[A-Z_]+@", unit.read_text()))
        assert found and found <= known, (unit.name, found - known)
        assert found <= set(re.findall(r"@[A-Z_]+@", (DEPLOY / "render-units.sh").read_text()))
