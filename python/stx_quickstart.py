#!/usr/bin/env python3
"""
STX API quickstart - API key signing, market data, and an order round trip.

Self-contained: talks to the GraphQL endpoint and the WebSocket directly, with no
STX SDK dependency. Roughly 200 lines, most of it comments.

    pip install cryptography requests websockets

    python stx_quickstart.py markets        # public - no key needed
    python stx_quickstart.py book           # public - live order book over WebSocket
    python stx_quickstart.py orders         # signed read
    python stx_quickstart.py roundtrip      # signed: place a resting order, then cancel it

Credentials come from a profile in ~/.stx/credentials (see Configuration below);
choose one with `--profile <name>`, and it defaults to `[default]`:

    python stx_quickstart.py --profile ontario-staging orders

Create a key at Account -> API Keys, choosing read_write scope for `roundtrip`.
The private key is shown once and is never stored by the server.

Questions: https://discord.gg/yF9eVzPzNZ
"""

import asyncio
import base64
import configparser
import json
import os
import sys
import time

import requests
import websockets
from cryptography.hazmat.primitives import serialization

GRAPHQL_PATH = "/api/graphql"
SOCKET_PATH = "/socket/websocket"

# (region, env) -> hostname. Mirrors the host map in the STX Python SDK.
HOSTS = {
    ("ontario", "production"): "api.on.stxapp.ca",
    ("ontario", "staging"): "staging.on.sportsxapp.com",
}


# --------------------------------------------------------------------------
# Configuration
#
# Credentials live in ~/.stx/credentials, an INI file with one section per
# environment - the same file and profile mechanism the STX Python SDK uses,
# so anything set up here carries over when you move to the SDK:
#
#     [default]
#     region      = ontario
#     env         = staging
#     key_id      = bf98340d96993755d8a4974e31d2991a
#     private_key = ~/.stx/ontario-staging.pem
#
#     [ontario-production]
#     region      = ontario
#     env         = production
#     key_id      = 7c21ab...
#     private_key = ~/.stx/ontario-production.pem
#
# Pick a profile with --profile <name> or STX_PROFILE. Individual values can be
# overridden by STX_KEY_ID, STX_PRIVATE_KEY, STX_REGION, STX_ENV and STX_HOST,
# which take precedence over the file. Keep the file and every PEM at chmod 600.
# --------------------------------------------------------------------------

PROFILE_FILE = os.path.expanduser("~/.stx/credentials")


def load_profile(name=None):
    """Resolve config from env vars layered over the named profile."""
    name = name or os.environ.get("STX_PROFILE", "default")
    values = {}
    if os.path.exists(PROFILE_FILE):
        parser = configparser.ConfigParser()
        parser.read(PROFILE_FILE)
        if parser.has_section(name):
            values = dict(parser.items(name))
        elif name != "default":
            sys.exit(f"Profile {name!r} not found in {PROFILE_FILE}. "
                     f"Available: {parser.sections()}")

    def pick(env_var, key, default=None):
        return os.environ.get(env_var) or values.get(key) or default

    region = pick("STX_REGION", "region", "ontario")
    env = pick("STX_ENV", "env", "staging")
    host = pick("STX_HOST", "host") or HOSTS.get((region, env))
    if not host:
        extra = (" US endpoints are not published yet - set `host` explicitly."
                 if region == "us" else "")
        sys.exit(f"No host known for region={region!r} env={env!r}.{extra} "
                 f"Set STX_HOST or `host` in the profile.")

    key_path = pick("STX_PRIVATE_KEY", "private_key", "~/.stx/my_key.pem")
    return {
        "profile": name,
        "host": host,
        "http_base": f"https://{host}",
        "ws_base": f"wss://{host}",
        "key_id": pick("STX_KEY_ID", "key_id"),
        "key_path": os.path.expanduser(key_path),
    }


CONFIG = {}   # populated in __main__ once the profile is known


# --------------------------------------------------------------------------
# Signing
#
# Three headers per request. The signed message is a bare concatenation:
#
#     timestamp_ms + HTTP_METHOD_UPPERCASE + request_path
#
# No separators, no body. The path carries its query string when there is one,
# but never the scheme or host. Pure Ed25519 (RFC 8032) over the UTF-8 bytes --
# no pre-hashing, not the Ed25519ph variant -- base64 with standard alphabet
# and padding. The timestamp must land within 30 seconds of the server clock,
# so generate it per request and keep the machine on NTP.
#
# On the WebSocket the header names take an `X-` prefix, because the socket
# transport only surfaces `x-*` headers. There you sign method GET against the
# handshake path with the query string dropped: `<ts>GET/socket/websocket`.
# --------------------------------------------------------------------------

def load_private_key(path=None):
    path = path or CONFIG["key_path"]
    with open(path, "rb") as fh:
        return serialization.load_pem_private_key(fh.read(), password=None)


def signed_headers(private_key, key_id, method, path, prefix=""):
    timestamp = str(int(time.time() * 1000))
    message = f"{timestamp}{method.upper()}{path}".encode("utf-8")
    signature = base64.b64encode(private_key.sign(message)).decode()
    return {
        f"{prefix}STX-ACCESS-KEY": key_id,
        f"{prefix}STX-ACCESS-TIMESTAMP": timestamp,
        f"{prefix}STX-ACCESS-SIGNATURE": signature,
    }


def call(query, variables=None, authenticated=False):
    """POST a GraphQL operation, signing it when authenticated."""
    headers = {"Content-Type": "application/json"}
    if authenticated:
        if not CONFIG.get("key_id"):
            sys.exit("No key_id for profile %r. Set key_id in ~/.stx/credentials or STX_KEY_ID."
                     % CONFIG["profile"])
        headers.update(signed_headers(load_private_key(), CONFIG["key_id"], "POST", GRAPHQL_PATH))

    response = requests.post(
        CONFIG["http_base"] + GRAPHQL_PATH,
        json={"query": query, "variables": variables or {}},
        headers=headers,
        timeout=15,
    )
    response.raise_for_status()
    body = response.json()
    if body.get("errors"):
        raise RuntimeError(json.dumps(body["errors"], indent=2))
    return body["data"]


# --------------------------------------------------------------------------
# Prices
#
# The two transports disagree on units, which is the single easiest way to be
# wrong by a factor of 100:
#
#     GraphQL             price = 4900   (integer cents)
#     order_book_update   p     = 49.0   (currency units, 2 dp)
#
# GraphQL rejects a non-integer price outright, so convert deliberately rather
# than passing socket values straight through.
# --------------------------------------------------------------------------

def cents(book_price):
    """Socket price (49.0) -> GraphQL price (4900)."""
    return int(round(float(book_price) * 100))


def dollars(graphql_price):
    """GraphQL price (4900) -> human/socket form (49.0)."""
    return graphql_price / 100.0


# --------------------------------------------------------------------------
# Public: market discovery. No authentication of any kind.
# --------------------------------------------------------------------------

MARKETS_QUERY = """
query Markets($input: MarketInfosInput) {
  marketInfos(input: $input) {
    marketId
    symbol
    title
    status
    sport
    maxPrice
    lastTradedPrice
    bids { price quantity }
    offers { price quantity }
  }
}
"""

# `sport` also carries non-sporting categories on some markets, so name the ones
# we want rather than excluding the ones we do not.
SPORTS = {"Baseball", "Basketball", "Soccer", "Football", "Hockey", "Cricket",
          "Tennis", "Golf", "MMA", "Boxing", "Racing"}


def open_markets(limit=10, sports_only=True):
    """Open markets, filtered server-side.

    `sports` and `status` are filters on the query itself, so the exchange does the
    work rather than us fetching everything and discarding most of it. Roughly 1,100
    markets are open at a time here, the large majority of them sports.
    """
    query_input = {"limit": limit, "status": ["OPEN"]}
    if sports_only:
        query_input["sports"] = sorted(SPORTS)
    return call(MARKETS_QUERY, {"input": query_input})["marketInfos"]


def has_book(market):
    return bool(market["bids"] or market["offers"])


def first_tradeable():
    """First market with a resting book: sports if any are trading, else anything.

    Liquidity moves around, so a sports market with a live book is not guaranteed at
    any given minute. Falling back keeps the examples runnable instead of failing on
    an empty book.
    """
    for sports_only in (True, False):
        for market in open_markets(limit=200, sports_only=sports_only):
            if has_book(market):
                return market
    return None


def cmd_markets():
    for m in open_markets(10):
        bid = m["bids"][0]["price"] if m["bids"] else None
        offer = m["offers"][0]["price"] if m["offers"] else None
        spread = f"{dollars(bid) if bid else '--':>7} / {dollars(offer) if offer else '--':<7}"
        print(f"{m['symbol']:<46} {spread}  max ${dollars(m['maxPrice']):>6.2f}  {m['title'][:34]}")


# --------------------------------------------------------------------------
# Public: live order book over the WebSocket.
#
# Phoenix channel protocol v2: every frame is
# [join_ref, ref, topic, event, payload]. The join_ref must match the one used
# for phx_join on subsequent messages to that topic, or the server ignores them.
# --------------------------------------------------------------------------

async def heartbeat(ws, interval=15):
    """Keep the socket alive.

    Sockets have a keep-alive timeout of about 20 seconds. Without these frames
    the server closes the connection, which looks like a silent stall from the
    client side. Any real integration needs this running for the life of the
    connection.
    """
    ref = 1000
    while True:
        await asyncio.sleep(interval)
        ref += 1
        await ws.send(json.dumps([None, str(ref), "phoenix", "heartbeat", {}]))


async def stream_book(market_id, updates=3, idle_timeout=30):
    topic = f"market:{market_id}"
    join_ref = "1"
    async with websockets.connect(f'{CONFIG["ws_base"]}{SOCKET_PATH}?vsn=2.0.0') as ws:
        keepalive = asyncio.create_task(heartbeat(ws))
        try:
            await ws.send(json.dumps([join_ref, "1", topic, "phx_join", {}]))
            # request_snapshot pushes the current book immediately, rather than
            # waiting for the next publish. Use it after every reconnect --
            # note that a market whose book has not changed publishes nothing,
            # so on a quiet market the snapshot may be all you get for a while.
            await asyncio.sleep(0.5)
            await ws.send(json.dumps([join_ref, "2", topic, "request_snapshot", {}]))

            seen = 0
            while seen < updates:
                try:
                    frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=idle_timeout))
                except asyncio.TimeoutError:
                    print(f"\n(no book change in {idle_timeout}s - the market is quiet; "
                          f"updates are published only when the book moves)")
                    return
                if frame[3] != "order_book_update":
                    continue
                book = frame[4]["ob"]
                seen += 1
                print(f"\n--- update {seen}  ({len(book['b'])} bids / {len(book['o'])} offers)")
                for level in book["b"][:5]:
                    # p price, q contracts, l liquidity (q*p),
                    # tc/tl cumulative contracts and liquidity through this level
                    print(f"  bid   {level['p']:>8.2f} x {level['q']:>8.0f}   cum {level['tc']:>8.0f}")
                for level in book["o"][:5]:
                    print(f"  offer {level['p']:>8.2f} x {level['q']:>8.0f}   cum {level['tc']:>8.0f}")
        finally:
            keepalive.cancel()


def cmd_book():
    market = first_tradeable()
    if not market:
        sys.exit("No market with a live book right now.")
    print(f"{market['symbol']}  {market['title']}")
    asyncio.run(stream_book(market["marketId"]))


# --------------------------------------------------------------------------
# Signed reads
# --------------------------------------------------------------------------

ORDERS_QUERY = """
query MyOrders($status: OrderStatus) {
  myOrderHistory(status: $status) {
    totalCount
    orders { id marketId action orderType price quantity filled status clientOrderId }
  }
}
"""


def cmd_orders():
    data = call(ORDERS_QUERY, {"status": "ACCEPTED"}, authenticated=True)
    result = data["myOrderHistory"]
    print(f"{result['totalCount']} open order(s)")
    for o in result["orders"]:
        print(f"  {o['id'][:8]}  {o['action']:<4} {o['quantity']:>5} @ "
              f"{dollars(o['price']) if o['price'] else 'MKT':<7} {o['status']:<10} {o['clientOrderId'] or ''}")


# --------------------------------------------------------------------------
# Signed round trip: rest an order well away from the touch, then cancel it.
#
# Deliberately priced not to fill. cancelOnDisconnect asks the exchange to pull
# the order if the connection dies, which is what you want on a quoting system;
# it pairs with a ping timeout on the active_orders channel.
# --------------------------------------------------------------------------

PLACE_MUTATION = """
mutation Place($userOrder: UserOrder!) {
  confirmOrder(userOrder: $userOrder) {
    errors
    order { id status price quantity filled clientOrderId }
  }
}
"""

CANCEL_MUTATION = """
mutation Cancel($orderId: rID!) {
  cancelOrder(orderId: $orderId) { status }
}
"""


def cmd_roundtrip():
    market = next((m for m in open_markets(limit=200) if m["bids"]), None) \
        or next((m for m in open_markets(limit=200, sports_only=False) if m["bids"]), None)
    if not market:
        sys.exit("No market with a bid to price against.")

    best_bid = market["bids"][0]["price"]          # integer cents
    price = max(1, best_bid - 1000)                # $10 below the touch: should rest, not fill
    print(f"{market['symbol']}  best bid ${dollars(best_bid):.2f} -> bidding ${dollars(price):.2f}")

    placed = call(PLACE_MUTATION, {
        "userOrder": {
            "marketId": market["marketId"],
            "orderType": "LIMIT",
            "action": "BUY",
            "price": price,                        # cents, integer, never a string
            "quantity": 1,
            "clientOrderId": f"quickstart-{int(time.time())}",
            "cancelOnDisconnect": True,
        },
    }, authenticated=True)["confirmOrder"]

    if placed.get("errors"):
        sys.exit(f"rejected: {placed['errors']}")

    order = placed["order"]
    print(f"placed  {order['id']}  status={order['status']}  filled={order['filled']}")

    cancelled = call(CANCEL_MUTATION, {"orderId": order["id"]}, authenticated=True)
    print(f"cancel  status={cancelled['cancelOrder']['status']}")


COMMANDS = {
    "markets": cmd_markets,
    "book": cmd_book,
    "orders": cmd_orders,
    "roundtrip": cmd_roundtrip,
}

if __name__ == "__main__":
    args = sys.argv[1:]
    profile = None
    if "--profile" in args:
        i = args.index("--profile")
        try:
            profile = args[i + 1]
        except IndexError:
            sys.exit("--profile needs a value")
        del args[i:i + 2]

    name = args[0] if args else "markets"
    if name not in COMMANDS:
        sys.exit(f"usage: {sys.argv[0]} [--profile NAME] [{' | '.join(COMMANDS)}]")

    CONFIG.update(load_profile(profile))
    print(f"[profile {CONFIG['profile']} -> {CONFIG['host']}]\n", file=sys.stderr)
    COMMANDS[name]()
