from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from nivesh_core.errors import ConfigError


def read_mapping(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"{path}: file not found")
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: invalid YAML ({type(e).__name__})") from e
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return data


M = TypeVar("M", bound=BaseModel)


def validate(model: type[M], data: dict[str, Any], path: Path) -> M:
    """Validate, converting pydantic errors to `loc: msg` lines (inputs never echoed)."""
    try:
        return model.model_validate(data)
    except ValidationError as e:
        lines = [f"  {'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()]
        raise ConfigError(f"{path}: invalid configuration\n" + "\n".join(lines)) from None
