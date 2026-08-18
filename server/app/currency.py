"""Which currency a visitor is quoted in.

Resolution mirrors the language layer on purpose: explicit ?cur= (and it
sticks) -> cookie -> country -> default. Someone who picked a currency meant
it, even on a connection that geolocates elsewhere.

Country comes from Cloudflare's CF-IPCountry header, which is already in front
of every request. No IP database to ship or keep current, and no third-party
lookup on the hot path.

The price tables are per-currency FILES, not a base price times a live rate.
A quoted price that moves with an exchange feed is a support ticket waiting to
happen — someone screenshots $10 and is charged $10.40 an hour later. These are
fixed price points; changing them is a deliberate edit to config/pricing.*.json.
"""
from __future__ import annotations

SUPPORTED = ("USD", "CNY", "EUR", "GBP", "HKD", "JPY")
DEFAULT = "USD"
COOKIE = "dhc_cur"
COOKIE_MAX_AGE = 365 * 24 * 3600

SYMBOL = {"USD": "$", "CNY": "¥", "EUR": "€", "GBP": "£", "HKD": "HK$", "JPY": "¥"}

# Only countries where quoting the local currency is clearly right. Everything
# else sees USD rather than a currency the buyer has to convert in their head.
BY_COUNTRY = {
    "CN": "CNY",
    "HK": "HKD", "MO": "HKD",
    "JP": "JPY",
    "GB": "GBP",
    "US": "USD", "CA": "USD", "AU": "USD", "SG": "USD",
}
EUROZONE = {"AT", "BE", "CY", "DE", "EE", "ES", "FI", "FR", "GR", "HR", "IE",
            "IT", "LT", "LU", "LV", "MT", "NL", "PT", "SI", "SK"}


def from_country(code: str) -> str | None:
    code = (code or "").strip().upper()
    if not code:
        return None
    if code in EUROZONE:
        return "EUR"
    return BY_COUNTRY.get(code)


def resolve(request) -> tuple[str, bool]:
    """(currency, explicit) — `explicit` means persist it as a cookie."""
    q = (request.query_params.get("cur") or "").strip().upper()
    if q in SUPPORTED:
        return q, True
    c = (request.cookies.get(COOKIE) or "").strip().upper()
    if c in SUPPORTED:
        return c, False
    return from_country(request.headers.get("cf-ipcountry", "")) or DEFAULT, False


def symbol(cur: str) -> str:
    return SYMBOL.get(cur, cur + " ")


def glyph(cur: str) -> str:
    """The symbol without its country qualifier: HK$ -> $.

    For a PRICE the qualifier is load-bearing — "$780" next to a Hong Kong
    price reads as US dollars. In a list that already names the currency it is
    noise, and it is the reason the picker's HKD row looked different from
    every other row. CNY and JPY have shown the same ¥ from the start, so the
    code beside it is what disambiguates there too.
    """
    return SYMBOL.get(cur, cur).lstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ") or SYMBOL.get(cur, cur)


def price_file(cur: str) -> str:
    cur = cur if cur in SUPPORTED else DEFAULT
    return f"pricing.{cur.lower()}.json"
