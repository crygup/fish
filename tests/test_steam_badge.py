from extensions.steam import (
    STEAM_BADGE_AVERAGE_COST_USD,
    _steam_badge_estimate,
    _steam_level_xp,
)


def test_steam_level_xp_uses_ten_level_tiers() -> None:
    assert _steam_level_xp(0) == 0
    assert _steam_level_xp(10) == 1_000
    assert _steam_level_xp(20) == 3_000
    assert _steam_level_xp(55) == 18_000
    assert _steam_level_xp(100) == 55_000


def test_steam_badge_estimate_counts_badges_and_cost() -> None:
    xp, badges, cost = _steam_badge_estimate(20, 30)

    assert xp == 3_000
    assert badges == 30
    assert cost == round(badges * STEAM_BADGE_AVERAGE_COST_USD, 2)
