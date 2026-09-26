"""The two tools the hello agent can call: a safe arithmetic calculator and a mock
weather lookup. Schemas are in Anthropic tool format (name, description, input_schema)."""

from __future__ import annotations

import ast
import operator
from typing import Any

CALCULATOR_SCHEMA = {
    "name": "calculator",
    "description": "Evaluate a basic arithmetic expression: + - * / // % ** and parentheses.",
    "input_schema": {
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "An arithmetic expression, e.g. '23 * 19' or '(2 + 3) ** 2'.",
            }
        },
        "required": ["expression"],
    },
}

WEATHER_SCHEMA = {
    "name": "get_weather",
    "description": "Get the current weather for a location. Returns mock data.",
    "input_schema": {
        "type": "object",
        "properties": {"location": {"type": "string", "description": "City name, e.g. 'Paris'."}},
        "required": ["location"],
    },
}

TOOLS = [CALCULATOR_SCHEMA, WEATHER_SCHEMA]

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _eval(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        return _BIN_OPS[type(node.op)](_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_eval(node.operand))
    raise ValueError("unsupported expression")


def calculator(expression: str) -> str:
    """Safely evaluate an arithmetic expression (no names, calls, or attributes)."""
    try:
        result = _eval(ast.parse(expression, mode="eval"))
    except (ValueError, SyntaxError, ZeroDivisionError, TypeError) as exc:
        return f"error: {exc}"
    # Present whole numbers without a trailing .0.
    if isinstance(result, float) and result.is_integer():
        result = int(result)
    return str(result)


def get_weather(location: str) -> str:
    """Mock weather lookup — deterministic so tests don't depend on a real service."""
    return f"It is 72°F and sunny in {location}."


def dispatch(name: str, args: dict[str, Any]) -> str:
    if name == "calculator":
        return calculator(str(args.get("expression", "")))
    if name == "get_weather":
        return get_weather(str(args.get("location", "")))
    return f"error: unknown tool {name!r}"
