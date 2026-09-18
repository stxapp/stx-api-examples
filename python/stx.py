"""Shared plumbing for the Python examples: hosts, profiles, and request signing.

Three things live here because they must be identical everywhere and because a
base URL should appear exactly once in this repository per language:

  * ``BASE_URLS``   - region + env -> host. The one table for Python.
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
import platform
from decimal import Decimal

from cryptography.hazmat.primitives import serialization

# ---------------------------------------------------------------------------
# Hosts
#
# A profile names a region and an environment, never a hostname, so that this
# table is the only place one appears. Only the public environments are listed,
# and US production is not open yet; anything else goes in a `base_url` line.
#
# Markets settle at $1, so `max_price` is "1.0000" and quotes run $0.01-$0.99.
# Read `max_price` off the market rather than assuming it.
# ---------------------------------------------------------------------------

BASE_URLS = {
    ("us", "demo"): "https://demo.stxapp.io",
    ("ontario", "demo"): "https://demo.stxapp.ca",
    ("ontario", "prod"): "https://stxapp.ca",
}

# A host not in that table - a local server, a review app - is set with
# STX_BASE_URL, or a `base_url` line in the profile. It wins over the table:
#
#     STX_BASE_URL=http://localhost:8000 STX_ENV=local \
#         python python/rest/quickstart.py markets
#
# `env` is still required alongside a base_url, because it decides more than
# the host: `roundtrip` and `latency.py` refuse to place orders when env is
# `prod`. Point base_url at a real exchange and that guard is all that stands
# between an example and a live book, so set env truthfully.

# Earlier versions of ./configure wrote `exchange`, `environment` and
# `private_key`. They are still read, and translated, so an existing
# credentials file keeps working.
LEGACY_REGIONS = {"ca": "ontario"}
LEGACY_ENVS = {"integration": "demo", "production": "prod"}
KNOWN_KEYS = {"region", "env", "key_id", "key_file", "base_url",
              "exchange", "environment", "private_key"}

# Identify your client. NOT required: the API accepts a request with no
# User-Agent. It is recorded against your API key though, so a recognisable
# string is what lets support tell your traffic from everyone else's when you
# report a problem. Change the product name to your own and keep the shape:
#
#     <product>/<version> (<runtime>)
USER_AGENT = f"stx-api-examples/1.0 (python/{platform.python_version()})"

SOCKET_PATH = "/socket/websocket"
CREDENTIALS_PATH = os.path.expanduser(
    os.environ.get("STX_CREDENTIALS", "~/.stx/credentials")
)


def load_profile(name=None):
    """Resolve one profile from ~/.stx/credentials into a config dict.

    Environment variables win over the file, which is what you want in CI:
    STX_PROFILE, STX_REGION, STX_ENV, STX_KEY_ID, STX_KEY_FILE, STX_BASE_URL.
    """
    name = name or os.environ.get("STX_PROFILE", "default")
    values = {}
    has_section = False

    if os.path.exists(CREDENTIALS_PATH):
        # Inline comments, as in `env = demo  # demo | prod`, are stripped the
        # same way ./verify and stx.mjs strip them.
        parser = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
        parser.read(CREDENTIALS_PATH)
        if parser.has_section(name):
            has_section = True
            values = dict(parser.items(name))
        elif name != "default":
            sys.exit(
                f"Profile {name!r} not found in {CREDENTIALS_PATH}. "
                f"Available: {parser.sections() or 'none'}. Run ./configure {name}"
            )

    # A misspelt key is otherwise silently ignored, and the profile quietly
    # resolves to something you did not ask for.
    unknown = sorted(set(values) - KNOWN_KEYS)
    if unknown:
        print(f"warning: [{name}] has unrecognised keys {unknown}; "
              f"expected {sorted(KNOWN_KEYS - {'exchange', 'environment', 'private_key'})}",
              file=sys.stderr)

    def pick(env_vars, keys):
        for var in env_vars:
            if os.environ.get(var):
                return os.environ[var]
        for key in keys:
            if values.get(key):
                return values[key]
        return None

    region = pick(["STX_REGION", "STX_EXCHANGE"], ["region", "exchange"])
    env = pick(["STX_ENV", "STX_ENVIRONMENT"], ["env", "environment"])
    region = LEGACY_REGIONS.get(region, region)
    env = LEGACY_ENVS.get(env, env)

    # A trailing slash would produce //api/v1, which some routers 404 on.
    base_url = (pick(["STX_BASE_URL"], ["base_url"]) or "").rstrip("/")

    if base_url:
        if not env:
            sys.exit(
                f"Profile [{name}] sets base_url but no env. Add `env = <name>` "
                f"(`prod` if that host takes real money) so the order guard knows."
            )
    else:
        if not has_section and not region and not env:
            # No profile and no overrides: the documented zero-config path,
            # STX_KEY_ID and STX_KEY_FILE alone, against the US demo exchange.
            region, env = "us", "demo"
        base_url = BASE_URLS.get((region, env))
        if not base_url:
            known = ", ".join(f"{r}/{e}" for r, e in sorted(BASE_URLS))
            sys.exit(
                f"No host for region={region or '(not set)'!s} env={env or '(not set)'!s} "
                f"in profile [{name}]. "
                f"Known: {known}. For any other host add a base_url line."
            )

    key_id = pick(["STX_KEY_ID"], ["key_id"])
    key_file = pick(["STX_KEY_FILE", "STX_PRIVATE_KEY"], ["key_file", "private_key"])
    if not key_id or not key_file:
        sys.exit(
            f"Profile [{name}] has no key_id or key_file. Run ./configure {name}"
        )

    return {
        "profile": name,
        "region": region,
        "env": env,
        "base_url": base_url,
        "socket_url": socket_url(base_url),
        "key_id": key_id,
        "key_path": os.path.expanduser(key_file),
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
        f"  to the host for this region/env pair."
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
        "User-Agent": USER_AGENT,
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
# width is a MINIMUM, not a promise: a computed field such as a fee can carry
# more. Parse with a variable-scale decimal type - Decimal here - and never with
# a fixed-width reader.
#
# Not every number is money. `price_change24h` is a percentage and `points` are
# loyalty points; both stay plain JSON numbers. Convert what is an amount of
# money or a count of contracts, nothing else.
#
# Going the other way, `price` and `quantity` on POST /api/v1/orders must both
# be strings. An integer price is rejected with a 400 rather than guessed at,
# because a legacy client's 5600 meant $56.00 and reading it as $5,600.00 would
# be a 100x overprice. `quantity` refuses numbers for a different reason: a
# float arrives as an IEEE-754 double, so a sent 2.675 would rest on the book as
# 2.67499999999999982... Integers are exact, but accepting them while refusing
# floats is harder to state than to follow, so every number is a 400.
#
# An order price is a whole number of cents: at most two decimal places, not
# counting trailing zeros. "0.49" and "0.4900" are accepted, "0.495" is a 400.
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
    """A price as the dollar string the API takes for an order: "0.51" -> "0.5100".

    Four decimals, matching the width the server echoes back. The input side is
    looser than the output - "0.51" and "0.5100" are the same order - so you
    never have to match the server's width.

    A price must be a whole number of cents. Anything finer, such as "0.495",
    raises here instead of being rounded, because rounding would quietly place a
    different order, and sending it is a 400 from the server anyway.
    """
    value = Decimal(str(value))
    if value != value.quantize(Decimal("0.01")):
        raise ValueError(
            f"price {value} is not a whole number of cents; "
            f"prices take at most two decimal places"
        )
    return f"{value:.4f}"

