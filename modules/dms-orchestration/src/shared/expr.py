"""Tiny, safe expression language for job manifests.

Used in two places of ``jobset.yaml``:

* ``when: "pre.schema-diff.summary.missingTables > 0"`` — run the job only if true;
* ``{{ ctx.resetTables }}`` inside ``command`` / ``with`` strings — templating.

Grammar (no function calls, no attribute access on Python objects, no eval)::

    expr    := or
    or      := and ("or" and)*
    and     := not ("and" not)*
    not     := "not" not | cmp
    cmp     := atom (("=="|"!="|">"|">="|"<"|"<="|"in"|"not in") atom)?
    atom    := number | 'string' | "string" | true | false | null | path | list | "(" expr ")"
    path    := name ("." name | "[" int "]")*      name may contain '-' (job names)
    list    := "[" (expr ("," expr)*)? "]"

A missing path evaluates to ``None``; comparisons with ``None`` other than
``==``/``!=`` are false. ``when`` can be wrapped in ``{{ }}`` or written bare.
"""

from __future__ import annotations

import json
import re
from typing import Any

_TOKEN = re.compile(
    r"""
    \s*(?:
      (?P<num>-?\d+(?:\.\d+)?)
    | (?P<str>'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")
    | (?P<op>==|!=|>=|<=|>|<|\(|\)|\[|\]|,|\.)
    | (?P<name>[A-Za-z_][A-Za-z0-9_-]*)
    )""",
    re.VERBOSE,
)
_TEMPLATE = re.compile(r"\{\{\s*(.*?)\s*\}\}")


class ExprError(ValueError):
    pass


def _tokenize(text: str) -> list[tuple[str, Any]]:
    tokens: list[tuple[str, Any]] = []
    pos = 0
    text = text.rstrip()
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            raise ExprError(f"unexpected character at {pos} in {text!r}")
        pos = m.end()
        if m.group("num") is not None:
            raw = m.group("num")
            tokens.append(("lit", float(raw) if "." in raw else int(raw)))
        elif m.group("str") is not None:
            raw = m.group("str")
            tokens.append(("lit", raw[1:-1].encode().decode("unicode_escape")))
        elif m.group("op") is not None:
            tokens.append(("op", m.group("op")))
        else:
            name = m.group("name")
            if name in ("true", "false", "null"):
                tokens.append(("lit", {"true": True, "false": False, "null": None}[name]))
            elif name in ("and", "or", "not", "in"):
                tokens.append(("kw", name))
            else:
                tokens.append(("name", name))
    return tokens


class _Parser:
    def __init__(self, tokens: list[tuple[str, Any]], scope: dict[str, Any]):
        self.tokens = tokens
        self.i = 0
        self.scope = scope

    def peek(self) -> tuple[str, Any] | None:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def take(self, kind: str | None = None, value: Any = None) -> tuple[str, Any]:
        tok = self.peek()
        if tok is None or (kind and tok[0] != kind) or (value is not None and tok[1] != value):
            raise ExprError(f"expected {value or kind}, got {tok}")
        self.i += 1
        return tok

    def accept(self, kind: str, value: Any) -> bool:
        tok = self.peek()
        if tok and tok[0] == kind and tok[1] == value:
            self.i += 1
            return True
        return False

    def parse(self) -> Any:
        value = self.or_()
        if self.peek() is not None:
            raise ExprError(f"unexpected token {self.peek()}")
        return value

    def or_(self) -> Any:
        value = self.and_()
        while self.accept("kw", "or"):
            right = self.and_()
            value = bool(value) or bool(right)
        return value

    def and_(self) -> Any:
        value = self.not_()
        while self.accept("kw", "and"):
            right = self.not_()
            value = bool(value) and bool(right)
        return value

    def not_(self) -> Any:
        if self.accept("kw", "not"):
            return not bool(self.not_())
        return self.cmp()

    def cmp(self) -> Any:
        left = self.atom()
        tok = self.peek()
        if tok is None:
            return left
        if tok == ("kw", "not"):
            nxt = self.tokens[self.i + 1] if self.i + 1 < len(self.tokens) else None
            if nxt == ("kw", "in"):
                self.i += 2
                right = self.atom()
                return not _contains(right, left)
            return left
        if tok == ("kw", "in"):
            self.i += 1
            return _contains(self.atom(), left)
        if tok[0] == "op" and tok[1] in ("==", "!=", ">", ">=", "<", "<="):
            self.i += 1
            right = self.atom()
            return _compare(tok[1], left, right)
        return left

    def atom(self) -> Any:
        tok = self.take()
        kind, value = tok
        if kind == "lit":
            return value
        if kind == "op" and value == "(":
            inner = self.or_()
            self.take("op", ")")
            return inner
        if kind == "op" and value == "[":
            items = []
            if not self.accept("op", "]"):
                items.append(self.or_())
                while self.accept("op", ","):
                    items.append(self.or_())
                self.take("op", "]")
            return items
        if kind == "name":
            current: Any = self.scope.get(value)
            while True:
                if self.accept("op", "."):
                    key = self.take("name")[1]
                    current = current.get(key) if isinstance(current, dict) else None
                elif self.accept("op", "["):
                    idx = self.take("lit")[1]
                    self.take("op", "]")
                    if isinstance(current, list) and isinstance(idx, int) and -len(current) <= idx < len(current):
                        current = current[idx]
                    elif isinstance(current, dict):
                        current = current.get(idx)
                    else:
                        current = None
                else:
                    return current
        raise ExprError(f"unexpected token {tok}")


def _contains(container: Any, item: Any) -> bool:
    if isinstance(container, (list, str, dict)):
        try:
            return item in container
        except TypeError:
            return False
    return False


def _compare(op: str, left: Any, right: Any) -> bool:
    if op == "==":
        return left == right
    if op == "!=":
        return left != right
    if left is None or right is None:
        return False
    try:
        return {
            ">": left > right,
            ">=": left >= right,
            "<": left < right,
            "<=": left <= right,
        }[op]
    except TypeError:
        return False


def evaluate(text: str, scope: dict[str, Any]) -> Any:
    stripped = text.strip()
    m = _TEMPLATE.fullmatch(stripped)
    if m:
        stripped = m.group(1)
    return _Parser(_tokenize(stripped), scope).parse()


def is_true(text: str | None, scope: dict[str, Any]) -> bool:
    if text is None or str(text).strip() == "":
        return True
    return bool(evaluate(text, scope))


def _to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ",".join(_to_text(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True)
    return str(value)


def render(value: Any, scope: dict[str, Any]) -> Any:
    """Render ``{{ expr }}`` placeholders in strings, lists and dicts.

    A string that is exactly one placeholder keeps the value's type (useful in
    ``with:``); otherwise values are converted to text (lists joined by ',').
    """
    if isinstance(value, str):
        m = _TEMPLATE.fullmatch(value.strip())
        if m:
            return evaluate(m.group(1), scope)
        return _TEMPLATE.sub(lambda mm: _to_text(evaluate(mm.group(1), scope)), value)
    if isinstance(value, list):
        return [render(v, scope) for v in value]
    if isinstance(value, dict):
        return {k: render(v, scope) for k, v in value.items()}
    return value


def render_command(command: list[str], scope: dict[str, Any]) -> list[str]:
    """Commands must stay a list of strings."""
    return [_to_text(render(part, scope)) for part in command]
