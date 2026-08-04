import pytest

from extensions.tools.calculator import (
    MathExpressionError,
    evaluate_math_expression,
    format_math_result,
)


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("2 + 2 * 3", 8),
        ("(2 + 3)^3", 125),
        ("sqrt(81) + sin(pi / 2)", 10.0),
        ("comb(10, 2) + factorial(5)", 165),
        ("clamp(-4, 0, 10) + gcd(24, 18)", 6),
        ("2×3 + 8÷4", 8.0),
    ],
)
def test_evaluate_math_expression(expression: str, expected: int | float) -> None:
    assert evaluate_math_expression(expression) == expected


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('whoami')",
        "open('/tmp/should-not-exist', 'w')",
        "(lambda: 1)()",
        "[1, 2, 3]",
        "2 ** 1001",
        "comb(1001, 2)",
        "perm(1001, 2)",
        "1 / 0",
    ],
)
def test_rejects_unsafe_or_unbounded_expression(expression: str) -> None:
    with pytest.raises(MathExpressionError):
        evaluate_math_expression(expression)


def test_format_math_result_avoids_unnecessary_float_noise() -> None:
    assert format_math_result(8.0) == "8"
    assert format_math_result(1 / 3) == "0.333333333333333"
