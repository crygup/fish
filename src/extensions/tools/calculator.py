from __future__ import annotations

import ast
import math
import operator
import re
from typing import TYPE_CHECKING, Any, Callable

import discord
from discord.ext import commands

from core import Cog

if TYPE_CHECKING:
    from extensions.context import Context


MAX_EXPRESSION_LENGTH = 1_000
MAX_AST_NODES = 150
MAX_POWER = 1_000
MAX_FACTORIAL = 1_000
MAX_RESULT_DIGITS = 1_000


class MathExpressionError(ValueError):
    """Raised when an expression is invalid or exceeds the safe limits."""


def _require_integer(value: int | float, function: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        if isinstance(value, float) and value.is_integer():
            return int(value)
        raise MathExpressionError(f"{function} requires integer arguments.")
    return value


def _factorial(value: int | float) -> int:
    number = _require_integer(value, "factorial")
    if not 0 <= number <= MAX_FACTORIAL:
        raise MathExpressionError(
            f"factorial arguments must be between 0 and {MAX_FACTORIAL}."
        )
    return math.factorial(number)


def _gcd(*values: int | float) -> int:
    if not values:
        raise MathExpressionError("gcd requires at least one argument.")
    return math.gcd(*(_require_integer(value, "gcd") for value in values))


def _lcm(*values: int | float) -> int:
    if not values:
        raise MathExpressionError("lcm requires at least one argument.")
    return math.lcm(*(_require_integer(value, "lcm") for value in values))


def _combination(n: int | float, r: int | float) -> int:
    number = _require_integer(n, "comb")
    choice = _require_integer(r, "comb")
    if not 0 <= number <= MAX_FACTORIAL or not 0 <= choice <= number:
        raise MathExpressionError(
            f"comb arguments must be between 0 and {MAX_FACTORIAL}."
        )
    return math.comb(number, choice)


def _permutation(n: int | float, r: int | float | None = None) -> int:
    number = _require_integer(n, "perm")
    choice = None if r is None else _require_integer(r, "perm")
    if not 0 <= number <= MAX_FACTORIAL or (
        choice is not None and not 0 <= choice <= number
    ):
        raise MathExpressionError(
            f"perm arguments must be between 0 and {MAX_FACTORIAL}."
        )
    return math.perm(number, choice)


def _round(value: int | float, digits: int | float = 0) -> int | float:
    places = _require_integer(digits, "round")
    if abs(places) > 100:
        raise MathExpressionError("round precision must be between -100 and 100.")
    return round(value, places)


def _log(value: int | float, base: int | float = math.e) -> float:
    return math.log(value, base)


def _clamp(value: int | float, low: int | float, high: int | float) -> int | float:
    if low > high:
        raise MathExpressionError("clamp requires its lower bound first.")
    return min(max(value, low), high)


def _safe_min(*values: int | float) -> int | float:
    if not values:
        raise MathExpressionError("min requires at least one argument.")
    return min(values)


def _safe_max(*values: int | float) -> int | float:
    if not values:
        raise MathExpressionError("max requires at least one argument.")
    return max(values)


SAFE_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "abs": abs,
    "acos": math.acos,
    "asin": math.asin,
    "atan": math.atan,
    "ceil": math.ceil,
    "clamp": _clamp,
    "comb": _combination,
    "cos": math.cos,
    "cosh": math.cosh,
    "cbrt": math.cbrt,
    "degrees": math.degrees,
    "exp": math.exp,
    "factorial": _factorial,
    "floor": math.floor,
    "gcd": _gcd,
    "hypot": math.hypot,
    "lcm": _lcm,
    "log": _log,
    "log10": math.log10,
    "log2": math.log2,
    "max": _safe_max,
    "min": _safe_min,
    "perm": _permutation,
    "radians": math.radians,
    "round": _round,
    "sin": math.sin,
    "sinh": math.sinh,
    "sqrt": math.sqrt,
    "tan": math.tan,
    "tanh": math.tanh,
}

SAFE_CONSTANTS: dict[str, int | float] = {
    "e": math.e,
    "inf": math.inf,
    "pi": math.pi,
    "tau": math.tau,
}

SAFE_BINARY_OPERATORS: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
SAFE_UNARY_OPERATORS: dict[type[ast.unaryop], Callable[[Any], Any]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def _check_result(value: Any) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MathExpressionError("Only real numeric results are supported.")
    if isinstance(value, float) and not math.isfinite(value):
        raise MathExpressionError("The result must be finite.")
    if isinstance(value, int) and len(str(abs(value))) > MAX_RESULT_DIGITS:
        raise MathExpressionError("That result is too large to display.")
    if isinstance(value, float) and abs(value) > 1e100:
        raise MathExpressionError("That result is too large to display.")
    return value


class _SafeEvaluator:
    def __init__(self, tree: ast.Expression) -> None:
        self.nodes = 0
        self.tree = tree

    def evaluate(self) -> int | float:
        return _check_result(self.visit(self.tree.body))

    def visit(self, node: ast.AST) -> int | float:
        self.nodes += 1
        if self.nodes > MAX_AST_NODES:
            raise MathExpressionError("That expression is too complex.")
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise MathExpressionError("Only real numbers are supported.")
            return _check_result(node.value)
        if isinstance(node, ast.Name):
            try:
                return SAFE_CONSTANTS[node.id.lower()]
            except KeyError as error:
                raise MathExpressionError(f"Unknown name `{node.id}`.") from error
        if isinstance(node, ast.BinOp):
            operator_function = SAFE_BINARY_OPERATORS.get(type(node.op))
            if operator_function is None:
                raise MathExpressionError("That operator is not supported.")
            left = self.visit(node.left)
            right = self.visit(node.right)
            if isinstance(node.op, ast.Pow):
                if abs(right) > MAX_POWER or not float(right).is_integer():
                    raise MathExpressionError(
                        f"Exponents must be whole numbers between -{MAX_POWER} and {MAX_POWER}."
                    )
            try:
                return _check_result(operator_function(left, right))
            except (OverflowError, ZeroDivisionError, ValueError) as error:
                raise MathExpressionError("That operation is not defined.") from error
        if isinstance(node, ast.UnaryOp):
            operator_function = SAFE_UNARY_OPERATORS.get(type(node.op))
            if operator_function is None:
                raise MathExpressionError("That operator is not supported.")
            try:
                return _check_result(operator_function(self.visit(node.operand)))
            except (OverflowError, ValueError) as error:
                raise MathExpressionError("That operation is not defined.") from error
        if isinstance(node, ast.Call):
            if node.keywords or not isinstance(node.func, ast.Name):
                raise MathExpressionError("Only supported math functions can be used.")
            function = SAFE_FUNCTIONS.get(node.func.id.lower())
            if function is None:
                raise MathExpressionError(f"Unknown function `{node.func.id}`.")
            if len(node.args) > 32:
                raise MathExpressionError("That function has too many arguments.")
            try:
                return _check_result(
                    function(*(self.visit(argument) for argument in node.args))
                )
            except MathExpressionError:
                raise
            except (OverflowError, ZeroDivisionError, ValueError, TypeError) as error:
                raise MathExpressionError("That operation is not defined.") from error
        raise MathExpressionError(
            "Only numbers, operators, parentheses, and supported functions are allowed."
        )


def evaluate_math_expression(expression: str) -> int | float:
    """Evaluate a bounded arithmetic expression without executing user code."""
    expression = expression.strip()
    if not expression:
        raise MathExpressionError("Enter an expression to calculate.")
    if len(expression) > MAX_EXPRESSION_LENGTH:
        raise MathExpressionError(
            f"That expression is too long. Keep it under {MAX_EXPRESSION_LENGTH} characters."
        )
    if re.search(r"\d{101,}", expression):
        raise MathExpressionError("Numbers may contain at most 100 digits.")
    expression = (
        expression.replace("×", "*")
        .replace("÷", "/")
        .replace("π", "pi")
        .replace("^", "**")
    )
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as error:
        raise MathExpressionError("That is not a valid math expression.") from error
    return _SafeEvaluator(tree).evaluate()


def format_math_result(value: int | float) -> str:
    if isinstance(value, int):
        return str(value)
    if value == 0:
        return "0"
    return format(value, ".15g")


class Calculator(Cog):
    emoji = discord.PartialEmoji(name="➗")

    @commands.hybrid_command(name="math", aliases=("calc", "calculate"))
    async def math(self, ctx: Context, *, expression: str):
        """Calculate a complex arithmetic expression safely."""
        try:
            result = evaluate_math_expression(expression)
        except MathExpressionError as error:
            raise commands.BadArgument(str(error)) from error
        await ctx.send(f"`{format_math_result(result)}`")
