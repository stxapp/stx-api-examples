"""Shared plumbing for the Python examples: hosts, profiles, and request signing.

Three things live here because they must be identical everywhere and because a
base URL should appear exactly once in this repository per language:

  * ``BASE_URLS``   - exchange + environment -> host. The one table for Python.
  * ``load_profile`` - reads ~/.stx/credentials, the file ``./configure`` writes.
  * ``signed_headers`` - the Ed25519 signing scheme, in about ten lines.

Everything else is in the example scripts themselves, which are meant to be read
top to bottom and copied.
"""

import base64
import configparser
import os
import sys
import time
from decimal import Decimal

from cryptography.hazmat.primitives import serialization

# ---------------------------------------------------------------------------
# Hosts
#
# `configure` stores an exchange and an environment, never a hostname, so that
# this table is the only place one appears.
#
# The US exchange settles markets at $1, so `max_price` is "1.0000" and quotes
# run $0.01-$0.99. Canada settles at $100, so `max_price` is "100.0000" there.
# Read `max_price` off the market rather than assuming either.
# ---------------------------------------------------------------------------

BASE_URLS = {
    ("us", "integration"): "https://demo.stxapp.io",
    ("ca", "integration"): "https://api-staging.on.sportsxapp.com",
    ("ca", "production"): "https://api.on.stxapp.ca",
}

# A host not in that table - a local server, a review app - is set with
# STX_BASE_URL, or a `base_url` line in the profile. It wins over the pair:
#
#     STX_BASE_URL=http://localhost:8000 python python/rest/quickstart.py markets
#
# `exchange` and `environment` still apply, because they decide more than the
# host: `roundtrip` and `latency.py` refuse to place orders when environment is
# `production`. Point base_url at a real exchange and that guard is all that
# stands between an example and a live book, so leave environment alone unless
# you mean it.
SOCKET_PATH = "/socket/websocket"
CREDENTIALS_PATH = os.path.expanduser(
    os.environ.get("STX_CREDENTIALS", "~/.stx/credentials")
)


def load_profile(name=None):
    """Resolve one profile from ~/.stx/credentials into a config dict.

    Environment variables win over the file, which is what you want in CI:
    STX_PROFILE, STX_EXCHANGE, STX_ENVIRONMENT, STX_KEY_ID, STX_PRIVATE_KEY,
    STX_BASE_URL.
    """
    name = name or os.environ.get("STX_PROFILE", "default")
    values = {}

    if os.path.exists(CREDENTIALS_PATH):
        parser = configparser.ConfigParser()
        parser.read(CREDENTIALS_PATH)
        if parser.has_section(name):
            values = dict(parser.items(name))
        elif name != "default":
            sys.exit(
                f"Profile {name!r} not found in {CREDENTIALS_PATH}. "
                f"Available: {parser.sections() or 'none'}. Run ./configure {name}"
            )

    def pick(env_var, key, default=None):
        return os.environ.get(env_var) or values.get(key) or default

    exchange = pick("STX_EXCHANGE", "exchange", "us")
    environment = pick("STX_ENVIRONMENT", "environment", "integration")

    # A trailing slash would produce //api/v1, which some routers 404 on.
    base_url = (pick("STX_BASE_URL", "base_url") or "").rstrip("/")
    if not base_url:
        base_url = BASE_URLS.get((exchange, environment))
    if not base_url:
        known = ", ".join(f"{e}/{v}" for e, v in sorted(BASE_URLS))
        sys.exit(
            f"No host for exchange={exchange!r} environment={environment!r}. "
            f"Known: {known}. Set STX_BASE_URL to use a host that is not in that list."
        )

    key_id = pick("STX_KEY_ID", "key_id")
    key_path = pick("STX_PRIVATE_KEY", "private_key")
    if not key_id or not key_path:
        sys.exit(
            f"Profile [{name}] has no key_id or private_key. Run ./configure {name}"
        )

    return {
        "profile": name,
        "exchange": exchange,
        "environment": environment,
        "base_url": base_url,
        "socket_url": socket_url(base_url),
        "key_id": key_id,
        "key_path": os.path.expanduser(key_path),
    }


def socket_url(base_url):
    """The WebSocket URL for an API base URL.

    http maps to ws as well as https to wss, so a local server on plain http
    works. Mapping only https would leave the scheme untouched and the socket
    would fail to connect with no useful message.
    """
    for http, ws in (("https://", "wss://"), ("http://", "ws://")):
        if base_url.startswith(http):
            return ws + base_url[len(http):] + SOCKET_PATH
    sys.exit(f"base_url must start with http:// or https://, got {base_url!r}")


def unreachable(base_url, error):
    """The message for a request that never reached the host.

    Connection refused is the ordinary first result of pointing STX_BASE_URL at
    a server that is not running, so it gets a sentence rather than a traceback.
    """
    return (
        f"Cannot reach {base_url}\n"
        f"  {error}\n"
        f"  If that is a local server, check it is running and on that port.\n"
        f"  Unset STX_BASE_URL (or drop base_url from your profile) to go back\n"
        f"  to the host for this exchange/environment pair."
    )


def load_private_key(config):
    with open(config["key_path"], "rb") as handle:
        return serialization.load_pem_private_key(handle.read(), password=None)


# ---------------------------------------------------------------------------
# Signing
#
# Three headers on every /api/v1 call. Every route needs them, so this runs on
# every request you will ever make.
#
#     X-STX-ACCESS-KEY         your key id
#     X-STX-ACCESS-TIMESTAMP   Unix milliseconds, as a string
#     X-STX-ACCESS-SIGNATURE   base64 Ed25519 signature of the message below
#
# The message is a bare concatenation, with no separators:
#
#     timestamp_ms + HTTP_METHOD_UPPERCASE + path
#
# The body is NOT signed. The path carries its query string when there is one -
# `/api/v1/markets?status=open` signs with the query attached - but never the
# scheme or host. Plain Ed25519 (RFC 8032) over the UTF-8 bytes, not the
# Ed25519ph pre-hashed variant, base64-encoded with the standard alphabet and
# padding.
#
# The timestamp must be within 30 seconds of the server clock, so generate it
# per request and keep the machine on NTP. A clock 40 seconds fast fails every
# request with a 401 that looks exactly like a bad key.
#
# The WebSocket handshake signs the same way, with one difference: the path is
# `/socket/websocket` with any query string DROPPED, and the method is GET.
# ---------------------------------------------------------------------------


def signed_headers(private_key, key_id, method, path):
    timestamp = str(int(time.time() * 1000))
    message = f"{timestamp}{method.upper()}{path}".encode("utf-8")
    signature = base64.b64encode(private_key.sign(message)).decode()
    return {
        "X-STX-ACCESS-KEY": key_id,
        "X-STX-ACCESS-TIMESTAMP": timestamp,
        "X-STX-ACCESS-SIGNATURE": signature,
    }


# ---------------------------------------------------------------------------
# Money and quantities
#
# Every money and quantity field on /api/v1 is a fixed-point DECIMAL STRING, in
# dollars. Not cents, not a JSON number:
#
#   market["max_price"]            "1.0000"    $1, a US market's ceiling
#   market["bids"][0]["price"]     "0.6100"    $0.61
#   market["bids"][0]["quantity"]  "491.00"    contracts
#   order["price"]                 "0.5100"    $0.51, or null on a market order
#   order["quantity"], ["filled"]  "1.00"      contracts
#
# Money carries at least four decimals and quantities at least two, but the
# width is a MINIMUM, not a promise: an order price can carry seven. Parse with
# a variable-scale decimal type - Decimal here - and never with a fixed-width
# reader.
#
# Not every number is money. `price_change24h` is a percentage and `points` are
# loyalty points; both stay plain JSON numbers. Convert what is an amount of
# money or a count of contracts, nothing else.
#
# Going the other way, `price` on POST /api/v1/orders must be a string. An
# integer is rejected with a 400 rather than guessed at, because a legacy
# client's 5600 meant $56.00 and reading it as $5,600.00 would be a 100x
# overprice. `quantity` still accepts a number, since a contract count has no
# unit ambiguity.
# ---------------------------------------------------------------------------


def to_decimal(value):
    """One money or quantity field as a ``Decimal``. ``None`` passes through.

    ``Decimal`` and not ``float``: the wire value is exact and decimal, and
    float64 is neither. ``float("0.61") * 3`` is 1.8299999999999998.
    """
    return None if value is None else Decimal(str(value))


def fmt_money(value, places=2):
    """One money field as a display string: "0.6100" -> "$0.61".

    Display only. Never build a request body from this - the wire wants
    ``dollar_string``, and a value rounded for a column is not the value.
    """
    return "-" if value is None else f"${to_decimal(value):.{places}f}"


def dollar_string(value):
    """A ``Decimal`` as the dollar string the API takes for an order price.

    At least four decimals, matching the width the server echoes back, but never
    fewer than the value carries: a price may hold up to seven, and rounding one
    off here would quietly place a different order. This mirrors how the server
    formats money on the way out.

    The input side is looser than the output - "0.51", "0.5100" and "0.510000"
    are the same order - so you never have to match the server's width.
    """
    value = Decimal(value)
    scale = max(4, -value.normalize().as_tuple().exponent)
    return f"{value:.{scale}f}"


# ---------------------------------------------------------------------------
# The legacy socket topics
#
# The pre-SX-12037 WebSocket topics were not converted and still send integer
# cents, and one `market:` join reply carries the book twice in two units
# (`ob` in dollars, `bids`/`offers` in cents). None of the examples here join
# them any more - they use the dollar topics, which agree with /api/v1 field
# for field. CHANNELS.md documents both and how they map onto each other.
# ---------------------------------------------------------------------------
