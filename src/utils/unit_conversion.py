from __future__ import annotations

import asyncio
import math
import re
from dataclasses import dataclass
from typing import Any, Literal

import pycountry
from cachetools import TTLCache

ConversionKind = Literal["currency", "measurement"]

_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_CURRENCY_SYMBOLS = {
    "$": "USD",
    "€": "EUR",
    "£": "GBP",
    "¥": "JPY",
    "₹": "INR",
    "₽": "RUB",
    "₩": "KRW",
    "₺": "TRY",
    "₫": "VND",
    "₪": "ILS",
    "₴": "UAH",
    "฿": "THB",
    "₱": "PHP",
    "₦": "NGN",
    "₡": "CRC",
    "₲": "PYG",
}
_ROBUX_ALIASES = frozenset({"robux", "rbx", "robucks"})
_VBUCKS_ALIASES = frozenset({"vbucks", "vbuck"})
_CRYPTO_IDS = {
    "BTC": "bitcoin",
    "LTC": "litecoin",
    "ETH": "ethereum",
    "DOGE": "dogecoin",
    "TRUMP": "official-trump",
}
_CRYPTO_ALIASES = {
    "bitcoin": "BTC",
    "btc": "BTC",
    "xbt": "BTC",
    "litecoin": "LTC",
    "ltc": "LTC",
    "ethereum": "ETH",
    "ether": "ETH",
    "eth": "ETH",
    "dogecoin": "DOGE",
    "doge": "DOGE",
    "trump": "TRUMP",
    "officialtrump": "TRUMP",
}
# These are common stock symbols. Keeping the list explicit avoids treating
# measurement abbreviations such as ``m`` or ``lb`` as stock tickers.
_STOCK_ALIASES = {
    "tsla": "TSLA",
    "tesla": "TSLA",
    "aapl": "AAPL",
    "apple": "AAPL",
    "msft": "MSFT",
    "microsoft": "MSFT",
    "nvda": "NVDA",
    "nvidia": "NVDA",
    "amzn": "AMZN",
    "amazon": "AMZN",
    "googl": "GOOGL",
    "google": "GOOGL",
    "meta": "META",
    "spy": "SPY",
    "qqq": "QQQ",
    "gme": "GME",
    "amc": "AMC",
}
# These are retail-value estimates. Virtual-currency prices vary by region,
# platform, and package, so results involving either currency are approximate.
ROBUX_PER_USD = 80.0
VBUCKS_PER_USD = 800.0 / 8.99
# Roblox's current standard DevEx rate for Earned Robux.
DEVEX_USD_PER_ROBUX = 0.0038
# Currency rates are slow-moving and can be reused across command invocations.
_CURRENCY_RATE_CACHE: TTLCache[str, dict[str, float]] = TTLCache[str, dict[str, float]](
    maxsize=64, ttl=12 * 60 * 60
)
_MARKET_PRICE_CACHE: TTLCache[str, float] = TTLCache[str, float](
    maxsize=128, ttl=12 * 60 * 60
)


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _build_currency_aliases() -> dict[str, str]:
    aliases = {_normalize(symbol): code for symbol, code in _CURRENCY_SYMBOLS.items()}
    for currency in pycountry.currencies:
        code = getattr(currency, "alpha_3", None)
        name = getattr(currency, "name", None)
        if not isinstance(code, str) or not isinstance(name, str):
            continue
        aliases[_normalize(code)] = code
        aliases[_normalize(name)] = code

        # pycountry generally stores the singular display name, while users
        # commonly ask for a plural form such as "Argentine pesos". Add the
        # common plural alongside the canonical name without changing names
        # whose final word is not a currency unit.
        final_word = name.casefold().split()[-1]
        pluralizable = {
            "cent",
            "dollar",
            "dinar",
            "dirham",
            "dong",
            "euro",
            "franc",
            "gourde",
            "guarani",
            "krona",
            "krone",
            "lek",
            "lira",
            "metical",
            "naira",
            "peso",
            "pound",
            "real",
            "riyal",
            "ruble",
            "rupee",
            "shilling",
            "sol",
            "som",
            "taka",
            "tenge",
            "won",
            "yen",
            "yuan",
        }
        if final_word in pluralizable:
            aliases[_normalize(f"{name}s")] = code
        elif final_word.endswith("s"):
            aliases[_normalize(name[:-1])] = code

    aliases.update(
        {
            "buck": "USD",
            "bucks": "USD",
            "dollar": "USD",
            "dollars": "USD",
            "usdollar": "USD",
            "usdollars": "USD",
            "pound": "GBP",
            "pounds": "GBP",
            "sterling": "GBP",
            "quid": "GBP",
            "euro": "EUR",
            "euros": "EUR",
            "yen": "JPY",
            "yuan": "CNY",
            "renminbi": "CNY",
            "rupee": "INR",
            "rupees": "INR",
            "rubles": "RUB",
            "ruble": "RUB",
            "won": "KRW",
            "franc": "CHF",
            "francs": "CHF",
            "peso": "MXN",
            "pesos": "MXN",
            # "Argentine" is the canonical country adjective in pycountry,
            # but "Argentinian pesos" is a common user-facing spelling.
            "argentinianpeso": "ARS",
            "argentinianpesos": "ARS",
        }
    )
    aliases.update(
        {
            _normalize(alias): code
            for alias, code in {**_CRYPTO_ALIASES, **_STOCK_ALIASES}.items()
        }
    )
    return aliases


CURRENCY_ALIASES = _build_currency_aliases()


@dataclass(frozen=True, slots=True)
class Unit:
    category: Literal["length", "time", "temperature", "mass"]
    factor: float
    label: str


UNIT_ALIASES: dict[str, tuple[Unit, ...]] = {}


def _register_unit(
    category: Literal["length", "time", "temperature", "mass"],
    factor: float,
    label: str,
    *names: str,
) -> None:
    definition = Unit(category, factor, label)
    for name in (label, *names):
        UNIT_ALIASES.setdefault(_normalize(name), ())
        UNIT_ALIASES[_normalize(name)] += (definition,)


_register_unit(
    "length",
    0.001,
    "mm",
    "mm",
    "millimeter",
    "millimeters",
    "millimetre",
    "millimetres",
)
_register_unit(
    "length", 0.01, "cm", "cm", "centimeter", "centimeters", "centimetre", "centimetres"
)
_register_unit("length", 1.0, "m", "meter", "meters", "metre", "metres")
_register_unit(
    "length", 1000.0, "km", "km", "kilometer", "kilometers", "kilometre", "kilometres"
)
_register_unit("length", 0.0254, "in", "in", "inch", "inches")
_register_unit("length", 0.3048, "ft", "ft", "foot", "feet")
_register_unit("length", 0.9144, "yd", "yd", "yard", "yards")
_register_unit("length", 1609.344, "mi", "mi", "mile", "miles")
_register_unit("time", 0.001, "ms", "ms", "millisecond", "milliseconds")
_register_unit("time", 1.0, "s", "s", "sec", "secs", "second", "seconds")
_register_unit("time", 60.0, "min", "m", "min", "mins", "minute", "minutes")
_register_unit("time", 3600.0, "h", "h", "hr", "hrs", "hour", "hours")
_register_unit("time", 86400.0, "day", "d", "day", "days")
_register_unit("time", 604800.0, "week", "w", "week", "weeks")
_register_unit("time", 2592000.0, "month", "mo", "month", "months")
_register_unit("time", 31536000.0, "year", "yr", "year", "years")
_register_unit("temperature", 1.0, "°C", "c", "°c", "celsius")
_register_unit("temperature", 1.0, "°F", "f", "°f", "fahrenheit")
_register_unit("temperature", 1.0, "K", "k", "kelvin")
_register_unit("mass", 0.001, "mg", "mg", "milligram", "milligrams")
_register_unit("mass", 1.0, "g", "g", "gram", "grams")
_register_unit("mass", 1000.0, "kg", "kg", "kilogram", "kilograms")
_register_unit("mass", 28.349523125, "oz", "oz", "ounce", "ounces")
_register_unit("mass", 453.59237, "lb", "lbs", "pound", "pounds")
_register_unit("mass", 6350.29318, "st", "st", "stone", "stones")
_register_unit(
    "mass", 1_000_000.0, "t", "t", "tonne", "tonnes", "metric ton", "metric tons"
)
_register_unit("mass", 907_184.74, "ton", "ton", "tons", "short ton", "short tons")


@dataclass(frozen=True, slots=True)
class ConversionRequest:
    amount: float
    source: str
    target: str
    kind: ConversionKind
    source_label: str
    devex: bool = False


class ConversionError(ValueError):
    """A conversion was recognized but could not be completed."""


def _currency_code(value: str | None) -> str | None:
    if not value:
        return None
    if value in _CURRENCY_SYMBOLS:
        return _CURRENCY_SYMBOLS[value]
    normalized = _normalize(value)
    if normalized in _ROBUX_ALIASES:
        return "ROBUX"
    if normalized in _VBUCKS_ALIASES:
        return "VBUCKS"
    return CURRENCY_ALIASES.get(normalized)


def _market_kind(code: str) -> Literal["crypto", "stock"] | None:
    if code in _CRYPTO_IDS:
        return "crypto"
    if code in set(_STOCK_ALIASES.values()):
        return "stock"
    return None


def _unit_candidates(value: str | None) -> tuple[Unit, ...]:
    if not value:
        return ()
    return UNIT_ALIASES.get(_normalize(value), ())


def _split_expression(rest: str) -> tuple[str | None, str | None]:
    rest = rest.strip()
    if not rest:
        return None, None
    keyword = re.search(r"\b(?:to|into|in|as)\b", rest, re.IGNORECASE)
    if keyword:
        left = rest[: keyword.start()].strip()
        right = rest[keyword.end() :].strip()
        return left or None, right or None

    parts = rest.split()
    if len(parts) < 2:
        return None, parts[0] if parts else None
    return parts[0], " ".join(parts[1:])


def _parse_height(expression: str) -> ConversionRequest | None:
    match = re.fullmatch(
        rf"\s*({_NUMBER})\s*['′]\s*({_NUMBER})\s*(?:[\"″]|in(?:ches)?)?\s*(?:to|into|in|as)?\s*([A-Za-z]+)\s*",
        expression,
        re.IGNORECASE,
    )
    if not match:
        return None
    feet = float(match.group(1))
    inches = float(match.group(2))
    target = match.group(3)
    target_units = _unit_candidates(target)
    length_targets = [unit for unit in target_units if unit.category == "length"]
    if not length_targets:
        return None
    return ConversionRequest(
        amount=feet * 0.3048 + inches * 0.0254,
        source="m",
        target=target,
        kind="measurement",
        source_label=f"{feet:g}′{inches:g}″",
    )


def parse_conversion_expression(expression: str) -> ConversionRequest | None:
    """Parse a currency or measurement expression without touching media URLs."""

    expression = expression.strip()
    if (
        not expression
        or "http://" in expression.casefold()
        or "https://" in expression.casefold()
    ):
        return None
    height = _parse_height(expression)
    if height:
        return height

    amount_match = re.match(
        rf"\s*(?P<symbol>[$€£¥₹₽₩₺₫₪₴฿₱₦₡₲])?\s*(?P<amount>{_NUMBER})(?P<attached>[A-Za-z°]+)?\s*(?P<rest>.*)$",
        expression,
    )
    if not amount_match:
        return None
    amount = float(amount_match.group("amount"))
    symbol = amount_match.group("symbol")
    attached = amount_match.group("attached")
    rest = amount_match.group("rest").strip()
    if attached:
        rest = f"{attached} {rest}".strip()
    source_raw, target_raw = _split_expression(rest)
    devex = False
    if target_raw:
        target_parts = target_raw.split()
        if any(_normalize(part) == "devex" for part in target_parts):
            devex = True
            target_parts = [
                part for part in target_parts if _normalize(part) != "devex"
            ]
            target_raw = " ".join(target_parts) or "robux"
    inferred_source = _currency_code(symbol)
    if inferred_source and source_raw is None:
        source_raw = inferred_source

    target_currency = _currency_code(target_raw)
    source_currency = _currency_code(source_raw)

    # A bare asset expression such as ``1btc`` or ``100 robux`` is a request
    # to value that asset in USD. Keep explicit ``to`` expressions unchanged.
    if target_currency and source_currency is None:
        if devex and target_currency == "ROBUX":
            source_currency = target_currency
        else:
            source_currency, target_currency = target_currency, "USD"

    if target_currency and source_currency:
        return ConversionRequest(
            amount=amount,
            source=source_currency,
            target=target_currency,
            kind="currency",
            source_label=source_raw or source_currency,
            devex=devex,
        )

    source_units = _unit_candidates(source_raw)
    target_units = _unit_candidates(target_raw)
    for source_unit in source_units:
        for target_unit in target_units:
            if source_unit.category == target_unit.category:
                return ConversionRequest(
                    amount=amount,
                    source=source_raw or source_unit.label,
                    target=target_raw or target_unit.label,
                    kind="measurement",
                    source_label=f"{_format_number(amount)} {source_unit.label}",
                )
    return None


def _format_number(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return f"{int(round(value)):,}"
    if 0 < abs(value) < 0.01:
        return f"{value:,.6f}".rstrip("0").rstrip(".")
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def _temperature_to_celsius(value: float, label: str) -> float:
    normalized = _normalize(label)
    if normalized in {"f", "fahrenheit"}:
        return (value - 32.0) * 5.0 / 9.0
    if normalized in {"k", "kelvin"}:
        return value - 273.15
    return value


def _celsius_to_temperature(value: float, label: str) -> float:
    normalized = _normalize(label)
    if normalized in {"f", "fahrenheit"}:
        return value * 9.0 / 5.0 + 32.0
    if normalized in {"k", "kelvin"}:
        return value + 273.15
    return value


def _unit_for(value: str, category: str) -> Unit:
    candidates = [unit for unit in _unit_candidates(value) if unit.category == category]
    if not candidates:
        raise ConversionError(f"Unsupported {category} unit: {value}")
    return candidates[0]


async def _currency_rates(ctx: Any, base: str) -> dict[str, float]:
    try:
        return dict(_CURRENCY_RATE_CACHE[base])
    except KeyError:
        pass

    try:
        async with asyncio.timeout(10):
            async with ctx.session.get(
                f"https://open.er-api.com/v6/latest/{base}"
            ) as response:
                if response.status != 200:
                    raise ConversionError("The currency rate service is unavailable.")
                data = await response.json(content_type=None)
    except ConversionError:
        raise
    except Exception as error:
        raise ConversionError("The currency rate service is unavailable.") from error

    if not isinstance(data, dict) or data.get("result") != "success":
        raise ConversionError("The currency rate service is unavailable.")
    rates = data.get("rates")
    if not isinstance(rates, dict):
        raise ConversionError("The currency rate service returned no rates.")
    parsed = {
        str(code): float(rate)
        for code, rate in rates.items()
        if isinstance(code, str) and isinstance(rate, (int, float))
    }
    parsed[base] = 1.0
    _CURRENCY_RATE_CACHE[base] = parsed
    return parsed


async def _market_json(
    ctx: Any,
    url: str,
    *,
    params: dict[str, str] | None = None,
) -> Any:
    try:
        async with asyncio.timeout(10):
            async with ctx.session.get(
                url,
                params=params,
                headers={"User-Agent": "Fishie currency converter"},
            ) as response:
                if response.status != 200:
                    raise ConversionError("The market price service is unavailable.")
                return await response.json(content_type=None)
    except ConversionError:
        raise
    except Exception as error:
        raise ConversionError("The market price service is unavailable.") from error


async def _market_price_usd(ctx: Any, code: str) -> float:
    cache_key = f"market:{code}"
    try:
        return _MARKET_PRICE_CACHE[cache_key]
    except KeyError:
        pass

    market_kind = _market_kind(code)
    if market_kind == "crypto":
        coin_id = _CRYPTO_IDS[code]
        data = await _market_json(
            ctx,
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": coin_id, "vs_currencies": "usd"},
        )
        value = data.get(coin_id, {}).get("usd") if isinstance(data, dict) else None
    elif market_kind == "stock":
        if not re.fullmatch(r"[A-Z]{1,5}", code):
            raise ConversionError(f"Unsupported stock symbol: {code}")
        data = await _market_json(
            ctx,
            f"https://query1.finance.yahoo.com/v8/finance/chart/{code}",
            params={"range": "1d", "interval": "1d"},
        )
        try:
            meta = data["chart"]["result"][0]["meta"]
            value = meta.get("regularMarketPrice") or meta.get("chartPreviousClose")
        except (KeyError, IndexError, TypeError):
            value = None
    else:
        raise ConversionError(f"Unsupported market asset: {code}")

    if value is None or isinstance(value, bool):
        raise ConversionError(f"No USD price was found for {code}.")
    try:
        price = float(str(value))
    except (TypeError, ValueError) as error:
        raise ConversionError(f"No USD price was found for {code}.") from error
    if not math.isfinite(price) or price <= 0:
        raise ConversionError(f"No USD price was found for {code}.")
    _MARKET_PRICE_CACHE[cache_key] = price
    return price


async def _to_usd(ctx: Any, amount: float, currency: str) -> float:
    if currency == "USD":
        return amount
    if currency == "ROBUX":
        return amount / ROBUX_PER_USD
    if currency == "VBUCKS":
        return amount / VBUCKS_PER_USD
    if _market_kind(currency) is not None:
        return amount * await _market_price_usd(ctx, currency)
    rates = await _currency_rates(ctx, currency)
    usd_rate = rates.get("USD")
    if usd_rate is None:
        raise ConversionError(f"Unsupported currency: {currency}")
    return amount * usd_rate


async def _from_usd(ctx: Any, amount: float, currency: str) -> float:
    if currency == "USD":
        return amount
    if currency == "ROBUX":
        return amount * ROBUX_PER_USD
    if currency == "VBUCKS":
        return amount * VBUCKS_PER_USD
    if _market_kind(currency) is not None:
        return amount / await _market_price_usd(ctx, currency)
    rates = await _currency_rates(ctx, "USD")
    target_rate = rates.get(currency)
    if target_rate is None:
        raise ConversionError(f"Unsupported currency: {currency}")
    return amount * target_rate


async def convert_request(ctx: Any, request: ConversionRequest) -> str:
    if request.kind == "currency":
        source = request.source
        target = request.target
        if source == target and not request.devex:
            return f"{_format_number(request.amount)} {source} = {source}"
        usd = await _to_usd(ctx, request.amount, source)
        if request.devex:
            if target != "ROBUX":
                raise ConversionError("DevEx can only be calculated from Robux.")
            robux = usd * ROBUX_PER_USD
            converted = robux * DEVEX_USD_PER_ROBUX
            if source == "ROBUX":
                return (
                    f"{_format_number(request.amount)} ROBUX → "
                    f"${_format_number(converted)} DevEx"
                )
            return (
                f"{_format_number(request.amount)} {source} → "
                f"{_format_number(robux)} ROBUX → "
                f"${_format_number(converted)} DevEx"
            )

        converted = await _from_usd(ctx, usd, target)
        separator = (
            "≈"
            if {source, target} & {"ROBUX", "VBUCKS"}
            or _market_kind(source) is not None
            or _market_kind(target) is not None
            else "="
        )
        return (
            f"{_format_number(request.amount)} {source} {separator} "
            f"{_format_number(converted)} {target}"
        )

    source_candidates = _unit_candidates(request.source)
    target_candidates = _unit_candidates(request.target)
    source_unit: Unit | None = None
    target_unit: Unit | None = None
    for source_candidate in source_candidates:
        for target_candidate in target_candidates:
            if source_candidate.category == target_candidate.category:
                source_unit = source_candidate
                target_unit = target_candidate
                break
        if source_unit is not None:
            break
    if source_unit is None or target_unit is None:
        raise ConversionError("Those units cannot be converted to each other.")

    if source_unit.category == "temperature":
        converted = _celsius_to_temperature(
            _temperature_to_celsius(request.amount, request.source), target_unit.label
        )
    else:
        converted = request.amount * source_unit.factor / target_unit.factor
    return (
        f"{request.source_label} = {_format_number(converted)} " f"{target_unit.label}"
    )


def conversion_candidate(expression: str) -> bool:
    """Return whether an expression looks like a value conversion."""

    if "http://" in expression.casefold() or "https://" in expression.casefold():
        return False
    return bool(re.match(rf"^\s*(?:[$€£¥₹₽₩₺₫₪₴฿₱₦₡₲]\s*)?{_NUMBER}", expression))
