"""Minimal JSON Schema (draft 2020-12 subset) validator with default filling.

Lambda has no ``jsonschema`` package and the Lambda zip stays dependency-free, so
the schemas in ``shared/schemas`` use only the keywords implemented here. The
test suite checks that this validator agrees with the reference ``jsonschema``
library on every fixture.

Supported: type, enum, const, required, properties, additionalProperties,
patternProperties, items, minItems, maxItems, uniqueItems, minimum, maximum,
minLength, maxLength, pattern, not (its ``description`` is the error message), $ref (local
``#/$defs/...``), default.
"""

from __future__ import annotations

import copy
import json
import re
from functools import cache
from pathlib import Path
from typing import Any

SCHEMA_DIR = Path(__file__).parent / "schemas"

_TYPES = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "null": lambda v: v is None,
}


class ValidationError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


@cache
def load_schema(name: str) -> dict[str, Any]:
    return json.loads((SCHEMA_DIR / f"{name}.schema.json").read_text(encoding="utf-8"))


def _resolve(root: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    ref = schema.get("$ref")
    if not ref:
        return schema
    if not ref.startswith("#/"):
        raise ValueError(f"only local $ref is supported: {ref}")
    node: Any = root
    for part in ref[2:].split("/"):
        node = node[part]
    return _resolve(root, node)


def _check(root: dict[str, Any], schema: dict[str, Any], value: Any, path: str, errors: list[str]) -> None:
    schema = _resolve(root, schema)
    where = path or "$"

    expected = schema.get("type")
    if expected is not None:
        types = expected if isinstance(expected, list) else [expected]
        if not any(_TYPES[t](value) for t in types):
            errors.append(f"{where}: expected {'/'.join(types)}, got {type(value).__name__}")
            return
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{where}: {value!r} is not one of {schema['enum']}")
    if "const" in schema and value != schema["const"]:
        errors.append(f"{where}: must be {schema['const']!r}")
    if "not" in schema:
        inner: list[str] = []
        _check(root, schema["not"], value, path, inner)
        if not inner:
            reason = _resolve(root, schema["not"]).get("description")
            errors.append(f"{where}: {value!r} is not allowed" + (f": {reason}" if reason else ""))

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{where}: shorter than {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{where}: longer than {schema['maxLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{where}: does not match {schema['pattern']}")
    if _TYPES["number"](value):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{where}: less than {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{where}: greater than {schema['maximum']}")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{where}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{where}: more than {schema['maxItems']} items")
        if schema.get("uniqueItems"):
            seen = [json.dumps(v, sort_keys=True) for v in value]
            if len(seen) != len(set(seen)):
                errors.append(f"{where}: items are not unique")
        if "items" in schema:
            for i, item in enumerate(value):
                _check(root, schema["items"], item, f"{where}[{i}]", errors)
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{where}: missing required property {key!r}")
        props = schema.get("properties", {})
        patterns = schema.get("patternProperties", {})
        extra = schema.get("additionalProperties", True)
        for key, item in value.items():
            matched = False
            if key in props:
                matched = True
                _check(root, props[key], item, f"{where}.{key}", errors)
            for pattern, sub in patterns.items():
                if re.search(pattern, key):
                    matched = True
                    _check(root, sub, item, f"{where}.{key}", errors)
            if not matched:
                if extra is False:
                    errors.append(f"{where}: unexpected property {key!r}")
                elif isinstance(extra, dict):
                    _check(root, extra, item, f"{where}.{key}", errors)


def validate(schema_name: str, value: Any) -> None:
    root = load_schema(schema_name)
    errors: list[str] = []
    _check(root, root, value, "", errors)
    if errors:
        raise ValidationError(errors)


def _fill(root: dict[str, Any], schema: dict[str, Any], value: Any) -> Any:
    schema = _resolve(root, schema)
    if isinstance(value, dict):
        for key, sub in schema.get("properties", {}).items():
            sub_r = _resolve(root, sub)
            if key not in value and "default" in sub_r:
                value[key] = copy.deepcopy(sub_r["default"])
            if key in value:
                value[key] = _fill(root, sub_r, value[key])
    elif isinstance(value, list) and "items" in schema:
        return [_fill(root, schema["items"], v) for v in value]
    return value


def with_defaults(schema_name: str, value: Any) -> Any:
    """Return a copy of ``value`` with schema defaults filled in (recursively)."""
    root = load_schema(schema_name)
    return _fill(root, root, copy.deepcopy(value))
