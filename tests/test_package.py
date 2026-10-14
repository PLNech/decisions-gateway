import tomllib
from pathlib import Path

import decisions_gateway

ROOT = Path(__file__).parent.parent


def test_version_matches_pyproject():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert decisions_gateway.__version__ == project["version"]
    assert project["license"] == "MIT" and project["dependencies"] == []
