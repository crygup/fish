from extensions.discord_ext.info import _badge_order_selectors


def test_badge_order_selectors_support_quotes_and_delimiters() -> None:
    assert _badge_order_selectors('"Legend of Table" 💰') == [
        "Legend of Table",
        "💰",
    ]
    assert _badge_order_selectors("custom:a, 🌽 | 💰") == [
        "custom:a",
        "🌽",
        "💰",
    ]


def test_badge_order_selectors_empty_value() -> None:
    assert _badge_order_selectors("") == []
