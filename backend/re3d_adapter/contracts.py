from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from .errors import ContractValidationError


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_ROOT = PROJECT_ROOT / "packages" / "contracts" / "schemas" / "v1"


@lru_cache(maxsize=None)
def contract_validator(name: str) -> Draft202012Validator:
    schema_path = SCHEMA_ROOT / f"{name}.schema.json"
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractValidationError(f"cannot load contract schema: {name}") from exc
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def validate_contract(name: str, value: Any) -> None:
    errors = sorted(
        contract_validator(name).iter_errors(value),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if not errors:
        return
    error = errors[0]
    location = ".".join(str(part) for part in error.absolute_path) or "<root>"
    raise ContractValidationError(
        f"{name} contract is invalid at {location}: {error.message}"
    )


def load_json_contract(path: Path, name: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ContractValidationError(f"cannot read {name} contract: {path.name}") from exc
    except json.JSONDecodeError as exc:
        raise ContractValidationError(
            f"{name} contract is not valid JSON: {path.name}"
        ) from exc
    if not isinstance(value, dict):
        raise ContractValidationError(f"{name} contract must be a JSON object")
    validate_contract(name, value)
    return value
