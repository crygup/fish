import pytest

from utils.timezone_locations import OfflineLocationResolver


def _timezones(results):
    return {result.timezone for result in results}


@pytest.fixture(scope="module")
def resolver() -> OfflineLocationResolver:
    return OfflineLocationResolver()


def test_resolves_country_names_and_codes(resolver: OfflineLocationResolver) -> None:

    assert _timezones(resolver.resolve("Germany")) == {"Europe/Berlin"}
    assert _timezones(resolver.resolve("DEU")) == {"Europe/Berlin"}
    assert {"America/New_York", "America/Chicago", "America/Los_Angeles"} <= (
        _timezones(resolver.resolve("U.S."))
    )


def test_resolves_us_states_and_multiple_state_timezones(
    resolver: OfflineLocationResolver,
) -> None:
    assert _timezones(resolver.resolve("California")) == {"America/Los_Angeles"}
    assert _timezones(resolver.resolve("Florida")) == {
        "America/Chicago",
        "America/New_York",
    }


def test_resolves_city_names_diacritics_and_alternate_names(
    resolver: OfflineLocationResolver,
) -> None:
    assert _timezones(resolver.resolve("Sao Paulo")) == {"America/Sao_Paulo"}
    assert _timezones(resolver.resolve("Bombay")) == {"Asia/Kolkata"}
    assert _timezones(resolver.resolve("Peking")) == {"Asia/Shanghai"}


def test_ambiguous_city_results_are_deduplicated_by_timezone(
    resolver: OfflineLocationResolver,
) -> None:
    results = resolver.resolve("Springfield")

    assert len(results) == len(_timezones(results))
    assert {"America/Chicago", "America/New_York", "America/Los_Angeles"} <= (
        _timezones(results)
    )
    assert all(len(result.label) <= 80 for result in results)


def test_country_state_collision_keeps_distinct_timezones(
    resolver: OfflineLocationResolver,
) -> None:
    assert _timezones(resolver.resolve("Georgia")) == {
        "America/New_York",
        "Asia/Tbilisi",
        "Europe/Moscow",
    }


def test_search_supports_prefix_autocomplete_and_limits_results(
    resolver: OfflineLocationResolver,
) -> None:
    results = resolver.search("Los Ang", limit=5)

    assert results
    assert results[0].timezone == "America/Los_Angeles"
    assert len(results) <= 5


def test_unknown_and_non_positive_limits_return_no_results(
    resolver: OfflineLocationResolver,
) -> None:
    assert resolver.resolve("not a real place") == []
    assert resolver.search("Berlin", limit=0) == []
