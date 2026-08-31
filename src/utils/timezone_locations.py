from __future__ import annotations

import bisect
import re
import unicodedata
from collections import defaultdict
from functools import lru_cache
from typing import Any, NamedTuple, cast

from geonamescache import GeonamesCache


class LocationTimezone(NamedTuple):
    """A user-facing location label and the IANA timezone it resolves to."""

    label: str
    timezone: str

    @property
    def key(self) -> str:
        """Alias used by the reminder command's existing ``TimeZone`` type."""

        return self.timezone


class _Candidate(NamedTuple):
    label: str
    timezone: str
    population: int
    kind_priority: int


_NON_WORD = re.compile(r"[^\w]+", re.UNICODE)


def _normalise(value: str) -> str:
    value = "".join(
        character
        for character in unicodedata.normalize("NFKD", value)
        if not unicodedata.combining(character)
    ).casefold()
    words = _NON_WORD.sub(" ", value).split()
    # Make dotted initialisms such as U.S. match their undotted forms.
    if len(words) > 1 and all(len(word) == 1 for word in words):
        return "".join(words)
    return " ".join(words)


def _choice_label(location: str, timezone: str) -> str:
    # Timezone ambiguity is presented with buttons, whose labels are limited
    # to 80 characters. Keep the timezone suffix intact when shortening.
    suffix = f" — {timezone}"
    return f"{location[: 80 - len(suffix)]}{suffix}"


class OfflineLocationResolver:
    """Resolve country, US state, and populated-city names without an API.

    ``geonamescache`` bundles the GeoNames cities dataset. Its city records
    already contain an IANA timezone, avoiding both network geocoding and the
    much larger polygon data needed by coordinate-based timezone finders.
    """

    def __init__(self, cache: GeonamesCache | None = None) -> None:
        self._cache = cache or GeonamesCache()
        self._countries = cast(dict[str, dict[str, Any]], self._cache.get_countries())
        self._states = cast(dict[str, dict[str, Any]], self._cache.get_us_states())
        cities = cast(list[dict[str, Any]], list(self._cache.get_cities().values()))

        self._country_by_alias: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for country in self._countries.values():
            for alias in (country["name"], country["iso"], country["iso3"]):
                self._country_by_alias[_normalise(str(alias))].append(country)

        self._state_by_alias: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for state in self._states.values():
            for alias in (state["name"], state["code"]):
                self._state_by_alias[_normalise(str(alias))].append(state)

        self._country_timezones: dict[str, dict[str, int]] = defaultdict(
            lambda: defaultdict(int)
        )
        self._state_timezones: dict[str, dict[str, int]] = defaultdict(
            lambda: defaultdict(int)
        )
        self._cities_by_alias: dict[str, list[_Candidate]] = defaultdict(list)
        for city in cities:
            timezone = str(city["timezone"])
            population = int(city["population"])
            country_code = str(city["countrycode"])
            state_code = str(city["admin1code"])
            self._country_timezones[country_code][timezone] += population
            if country_code == "US":
                self._state_timezones[state_code][timezone] += population

            candidate = _Candidate(
                _choice_label(self._location_for_city(city), timezone),
                timezone,
                population,
                0,
            )
            normalised = _normalise(str(city["name"]))
            if normalised:
                self._cities_by_alias[normalised].append(candidate)

        self._aliases = sorted(
            self._country_by_alias.keys()
            | self._state_by_alias.keys()
            | self._cities_by_alias.keys()
        )

    def _location_for_city(self, city: dict[str, Any]) -> str:
        country_code = str(city["countrycode"])
        country = self._countries.get(country_code)
        country_name = country["name"] if country is not None else country_code

        if country_code == "US":
            state = self._states.get(str(city["admin1code"]))
            if state is not None:
                return f"{city['name']}, {state['name']}, {country_name}"

        return f"{city['name']}, {country_name}"

    def _candidates(
        self, normalised: str, *, include_alternates: bool = True
    ) -> list[_Candidate]:
        candidates: list[_Candidate] = []
        administrative_match = False

        for country in self._country_by_alias.get(normalised, ()):
            administrative_match = True
            for timezone, population in self._country_timezones[
                str(country["iso"])
            ].items():
                location = f"{country['name']} (country)"
                candidates.append(
                    _Candidate(
                        _choice_label(location, timezone), timezone, population, 2
                    )
                )

        for state in self._state_by_alias.get(normalised, ()):
            administrative_match = True
            for timezone, population in self._state_timezones[
                str(state["code"])
            ].items():
                location = f"{state['name']}, United States (state)"
                candidates.append(
                    _Candidate(
                        _choice_label(location, timezone), timezone, population, 1
                    )
                )

        # Exact administrative names take precedence over cities which happen
        # to share that name (for example Florida, Cuba). Country/state
        # collisions such as Georgia remain visible and are disambiguated.
        if not administrative_match:
            candidates.extend(self._cities_by_alias.get(normalised, ()))
            if not candidates and include_alternates:
                candidates.extend(self._alternate_city_candidates(normalised))

        return candidates

    def _alternate_city_candidates(self, normalised: str) -> list[_Candidate]:
        """Scan translated city names only when a primary name did not match.

        Keeping every translated alias resident costs hundreds of megabytes.
        Exact alternate-name lookups are rare enough that a lazy scan is a
        better trade-off, and the result is cached by :meth:`resolve`.
        """

        matches: list[_Candidate] = []
        cities = cast(dict[str, dict[str, Any]], self._cache.get_cities())
        for city in cities.values():
            if not any(
                _normalise(str(alias)) == normalised
                for alias in city.get("alternatenames", ())
            ):
                continue
            timezone = str(city["timezone"])
            matches.append(
                _Candidate(
                    _choice_label(self._location_for_city(city), timezone),
                    timezone,
                    int(city["population"]),
                    0,
                )
            )
        return matches

    @staticmethod
    def _deduplicate(
        candidates: list[_Candidate], limit: int
    ) -> list[LocationTimezone]:
        # Selecting between two place-name interpretations is unnecessary when
        # both ultimately store the same IANA timezone.
        candidates.sort(
            key=lambda item: (-item.population, -item.kind_priority, item.label)
        )
        results: list[LocationTimezone] = []
        seen: set[str] = set()
        for candidate in candidates:
            if candidate.timezone in seen:
                continue
            seen.add(candidate.timezone)
            results.append(LocationTimezone(candidate.label, candidate.timezone))
            if len(results) == limit:
                break
        return results

    @lru_cache(maxsize=256)
    def resolve(self, query: str, limit: int = 25) -> list[LocationTimezone]:
        """Return exact location matches, ordered by represented population."""

        if limit < 1:
            return []
        return self._deduplicate(self._candidates(_normalise(query)), limit)

    @lru_cache(maxsize=256)
    def search(self, query: str, limit: int = 25) -> list[LocationTimezone]:
        """Return exact or prefix matches suitable for slash autocomplete."""

        if limit < 1:
            return []
        normalised = _normalise(query)
        if not normalised:
            return []

        # Autocomplete runs on every keystroke. Only the final conversion needs
        # the slower translated-alias scan; primary location prefixes are kept
        # resident and are effectively instant here.
        candidates = self._candidates(normalised, include_alternates=False)
        start = bisect.bisect_left(self._aliases, normalised)
        for alias in self._aliases[start:]:
            if not alias.startswith(normalised):
                break
            if alias != normalised:
                candidates.extend(self._candidates(alias, include_alternates=False))

        return self._deduplicate(candidates, limit)
