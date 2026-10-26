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


LAUNCHD_PLACEHOLDERS = {"@PYTHON@", "@HOME@", "@PREFIX@", "@BACKEND_DIR@", "@MODEL_DIR@"}


def test_launchd_templates_use_only_known_placeholders():
    script = (DEPLOY / "render-launchd.sh").read_text()
    for template in (DEPLOY / "launchd").iterdir():
        found = set(re.findall(r"@[A-Z_]+@", template.read_text()))
        assert found and found <= LAUNCHD_PLACEHOLDERS, (template.name, found - LAUNCHD_PLACEHOLDERS)
        assert found <= set(re.findall(r"@[A-Z_]+@", script))


def render_launchd(tmp_path, **env):
    import os
    import subprocess
    import sys

    stub = tmp_path / "model-server"
    (stub / "models").mkdir(parents=True, exist_ok=True)
    out = tmp_path / "agents"
    full = {**os.environ, "PYTHON": sys.executable, "BACKEND_DIR": str(stub), "DG_HOME": str(tmp_path), **env}
    return subprocess.run(
        ["sh", str(DEPLOY / "render-launchd.sh"), str(out)], env=full, capture_output=True, text=True
    ), out


def test_rendered_launchd_agents_are_valid_plists(tmp_path):
    import plistlib
    import shutil
    import subprocess

    done, out = render_launchd(tmp_path)
    assert done.returncode == 0, done.stderr
    gateway = plistlib.loads((out / "decisions-gateway.plist").read_bytes())
    backend = plistlib.loads((out / "backend.plist").read_bytes())
    assert gateway["Label"] == "decisions-gateway" and backend["Label"] == "decisions-backend"
    assert gateway["ProgramArguments"][-4:] == ["--host", "127.0.0.1", "--port", "8017"]
    assert "127.0.0.1" in backend["ProgramArguments"] and "8117" in backend["ProgramArguments"]
    for agent in (gateway, backend):
        assert agent["ProgramArguments"][0].startswith("/")
        assert agent["Umask"] == 0o077 and agent["KeepAlive"] == {"SuccessfulExit": False}
        assert agent["HardResourceLimits"]["Core"] == 0
        assert agent["StandardOutPath"].startswith(str(tmp_path) + "/Library/Logs/decisions-gateway/")
    if shutil.which("plutil"):
        for name in ("decisions-gateway.plist", "backend.plist"):
            assert subprocess.run(["plutil", "-lint", str(out / name)], capture_output=True).returncode == 0


def test_rendered_launchd_agents_survive_double_dash_paths(tmp_path):
    # Poetry venv names can hold "--", which XML forbids inside comments.
    import plistlib
    import sys

    odd = tmp_path / "venv--x" / "bin"
    odd.mkdir(parents=True)
    (odd / "python3").symlink_to(sys.executable)
    done, out = render_launchd(tmp_path, PYTHON=str(odd / "python3"), DG_HOME=str(tmp_path / "home--y"))
    assert done.returncode == 0, done.stderr
    for name in ("decisions-gateway.plist", "backend.plist"):
        plistlib.loads((out / name).read_bytes())


def test_render_launchd_refuses_unsafe_values(tmp_path):
    done, _ = render_launchd(tmp_path, BACKEND_DIR="/opt/a&b")
    assert done.returncode == 65 and "BACKEND_DIR" in done.stderr
    done, _ = render_launchd(tmp_path, PYTHON="relative/python3")
    assert done.returncode == 65 and "PYTHON" in done.stderr
