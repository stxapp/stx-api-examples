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

from cryptography.hazmat.primitives import serialization

# ---------------------------------------------------------------------------
# Hosts
#
# `configure` stores an exchange and an environment, never a hostname, so that
# this table is the only place one appears.
#
# The US exchange settles markets at $1: prices run 1-99 cents against a
# `max_price` of 100. Canada settles at $100, so `max_price` is 10000 there.
# Read `max_price` off the market rather than assuming either.
# ---------------------------------------------------------------------------

BASE_URLS = {
    ("us", "integration"): "https://demo.stxapp.io",
    ("ca", "integration"): "https://api-staging.on.sportsxapp.com",
    ("ca", "production"): "https://api.on.stxapp.ca",
}

SOCKET_PATH = "/socket/websocket"
CREDENTIALS_PATH = os.path.expanduser(
    os.environ.get("STX_CREDENTIALS", "~/.stx/credentials")
)


def load_profile(name=None):
    """Resolve one profile from ~/.stx/credentials into a config dict.

    Environment variables win over the file, which is what you want in CI:
    STX_PROFILE, STX_EXCHANGE, STX_ENVIRONMENT, STX_KEY_ID, STX_PRIVATE_KEY.
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

    base_url = BASE_URLS.get((exchange, environment))
    if not base_url:
        known = ", ".join(f"{e}/{v}" for e, v in sorted(BASE_URLS))
        sys.exit(
            f"No host for exchange={exchange!r} environment={environment!r}. Known: {known}"
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
        "socket_url": base_url.replace("https://", "wss://") + SOCKET_PATH,
        "key_id": key_id,
        "key_path": os.path.expanduser(key_path),
    }


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
# Prices
#
# The two halves of the API do not agree on units. This is the one place that
# reconciles them, so that no example has to remember which is which:
#
#   market["bids"][0]["price"]   "0.54"   decimal DOLLARS, sent as a string
#   socket book level["p"]       "0.54"   decimal DOLLARS
#   market["max_price"]          100      integer CENTS
#   order["price"]               54       integer CENTS
#
# Orders are placed and returned in cents, so any arithmetic against the touch
# has to convert first. Subtracting from the raw book value is a TypeError in
# Python and, worse, a silent -9.46 in JavaScript.
# ---------------------------------------------------------------------------


def book_price_cents(price):
    """A book or quote price as the integer cents that orders are priced in.

    Rounds halves up rather than to even, so that this agrees with
    ``bookPriceCents`` in javascript/stx.mjs on a tie - the two runtimes are
    meant to be directly comparable. Prices are never negative, so adding a
    half and truncating is a half-up round.
    """
    return int(float(price) * 100 + 0.5)
