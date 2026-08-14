from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

import utils.unit_conversion as unit_conversion
from utils.unit_conversion import (
    _CURRENCY_RATE_CACHE,
    _MARKET_PRICE_CACHE,
    convert_request,
    parse_conversion_expression,
)


class _Response:
    status = 200

    async def __aenter__(self) -> "_Response":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def json(self, **_kwargs: Any) -> dict[str, Any]:
        return {"result": "success", "rates": {"USD": 1, "GBP": 0.8, "EUR": 0.9}}


class _Session:
    def get(self, *_args: Any, **_kwargs: Any) -> _Response:
        return _Response()


def test_conversion_parser_accepts_currency_syntax_variants() -> None:
    expected = {
        "$10 to pounds": ("USD", "GBP"),
        "$10 into pounds": ("USD", "GBP"),
        "$10 pounds": ("USD", "GBP"),
        "10 usd to pounds": ("USD", "GBP"),
        "100 rbx to usd": ("ROBUX", "USD"),
        "1000 vbucks to Argentinian pesos": ("VBUCKS", "ARS"),
    }

    for expression, currencies in expected.items():
        request = parse_conversion_expression(expression)
        assert request is not None
        assert (request.source, request.target) == currencies


def test_conversion_parser_accepts_measurement_examples() -> None:
    for expression in (
        "10f to celsius",
        "12inch into miles",
        "1c to kelvin",
        "5'6 cm",
        "1 year into days",
        "5m into sec",
        "10lbs to ounces",
    ):
        request = parse_conversion_expression(expression)
        assert request is not None, expression
        assert request.kind == "measurement"


def test_measurement_conversion_handles_ambiguous_m_as_minutes() -> None:
    request = parse_conversion_expression("5m into sec")
    assert request is not None
    result = asyncio.run(convert_request(SimpleNamespace(session=None), request))
    assert result == "5 min = 300 s"


def test_height_conversion_handles_feet_and_inches() -> None:
    request = parse_conversion_expression("5'6 cm")
    assert request is not None
    result = asyncio.run(convert_request(SimpleNamespace(session=None), request))
    assert result == "5′6″ = 167.64 cm"


def test_weight_conversion_handles_pounds_and_ounces() -> None:
    request = parse_conversion_expression("10lbs to ounces")
    assert request is not None
    result = asyncio.run(convert_request(SimpleNamespace(session=None), request))
    assert result == "10 lb = 160 oz"


def test_currency_conversion_uses_cached_rates_and_robux() -> None:
    _CURRENCY_RATE_CACHE.clear()
    ctx = SimpleNamespace(session=_Session())

    pounds = parse_conversion_expression("10 usd to pounds")
    robux = parse_conversion_expression("100 robux to usd")
    assert pounds is not None and robux is not None
    assert asyncio.run(convert_request(ctx, pounds)) == "10 USD = 8 GBP"
    assert asyncio.run(convert_request(ctx, robux)) == "100 ROBUX ≈ 1.25 USD"


def test_currency_parser_accepts_vbucks_and_devex_chains() -> None:
    vbucks = parse_conversion_expression("100 v-bucks to usd")
    fiat_devex = parse_conversion_expression("10 usd to robux devex")
    robux_devex = parse_conversion_expression("1000 robux to devex")

    assert vbucks is not None
    assert (vbucks.source, vbucks.target, vbucks.devex) == ("VBUCKS", "USD", False)
    assert fiat_devex is not None
    assert (fiat_devex.source, fiat_devex.target, fiat_devex.devex) == (
        "USD",
        "ROBUX",
        True,
    )
    assert robux_devex is not None
    assert (robux_devex.source, robux_devex.target, robux_devex.devex) == (
        "ROBUX",
        "ROBUX",
        True,
    )


def test_currency_parser_defaults_single_assets_to_usd() -> None:
    for expression, source in (
        ("1btc", "BTC"),
        ("1 bitcoin", "BTC"),
        ("100 robux", "ROBUX"),
        ("1 eth", "ETH"),
        ("1 doge", "DOGE"),
        ("1 trump", "TRUMP"),
        ("1 tsla", "TSLA"),
    ):
        request = unit_conversion.parse_conversion_expression(expression)
        assert request is not None, expression
        assert (request.source, request.target) == (source, "USD")


@pytest.mark.asyncio
async def test_market_assets_convert_through_usd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _MARKET_PRICE_CACHE.clear()

    async def fake_market_json(
        _ctx: Any, url: str, *, params: dict[str, str] | None = None
    ) -> dict[str, Any]:
        if "coingecko" in url:
            assert params is not None
            return {params["ids"]: {"usd": 50_000}}
        assert url.endswith("/TSLA")
        return {"chart": {"result": [{"meta": {"regularMarketPrice": 250}}]}}

    monkeypatch.setattr(unit_conversion, "_market_json", fake_market_json)
    ctx = SimpleNamespace(session=_Session())

    bitcoin = unit_conversion.parse_conversion_expression("1btc")
    stock = unit_conversion.parse_conversion_expression("2 tsla to usd")
    assert bitcoin is not None and stock is not None
    assert await unit_conversion.convert_request(ctx, bitcoin) == "1 BTC ≈ 50,000 USD"
    assert await unit_conversion.convert_request(ctx, stock) == "2 TSLA ≈ 500 USD"


def test_market_aliases_are_not_measurement_units() -> None:
    measurement = unit_conversion.parse_conversion_expression("5m into sec")
    assert measurement is not None and measurement.kind == "measurement"


def test_virtual_currency_and_devex_conversion() -> None:
    ctx = SimpleNamespace(session=_Session())
    vbucks = parse_conversion_expression("100 vbucks to usd")
    usd_devex = parse_conversion_expression("10 usd to robux devex")
    robux_devex = parse_conversion_expression("1000 robux devex")

    assert vbucks is not None and usd_devex is not None and robux_devex is not None
    assert asyncio.run(convert_request(ctx, vbucks)) == "100 VBUCKS ≈ 1.12 USD"
    assert (
        asyncio.run(convert_request(ctx, usd_devex))
        == "10 USD → 800 ROBUX → $3.04 DevEx"
    )
    assert asyncio.run(convert_request(ctx, robux_devex)) == "1,000 ROBUX → $3.8 DevEx"
