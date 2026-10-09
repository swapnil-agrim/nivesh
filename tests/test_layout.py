import importlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PACKAGES = [
    "nivesh_core",
    "nivesh_engine",
    "nivesh_adapters",
    "nivesh_mcp",
    "nivesh_agents",
    "nivesh_cli",
]


@pytest.mark.parametrize("pkg", PACKAGES)
def test_package_imports(pkg: str) -> None:
    importlib.import_module(pkg)


@pytest.mark.parametrize("d", ["schemas", "prompts", "config", "docs/adr", "tests/fixtures"])
def test_dirs_exist(d: str) -> None:
    assert (ROOT / d).is_dir()


def test_readme_has_architecture_and_setup() -> None:
    text = (ROOT / "README.md").read_text()
    assert "## Architecture" in text
    assert "## Setup" in text
    assert "investright-mcp" in text  # deferred item is documented
