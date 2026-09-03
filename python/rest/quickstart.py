#!/usr/bin/env python3
"""STX REST quickstart - signed requests, market data, and an order round trip.

    python python/rest/quickstart.py me           # who this key belongs to
    python python/rest/quickstart.py markets      # markets with a resting book
    python python/rest/quickstart.py orders       # your open orders
    python python/rest/quickstart.py roundtrip    # place a resting order, then cancel it

Add ``--profile <name>`` to use a profile other than ``[default]``:

    python python/rest/quickstart.py --profile ca-integration markets

Credentials come from ~/.stx/credentials, written by ``./configure``. Every
/api/v1 route requires a signature, so even ``markets`` needs a key. ``roundtrip`` needs a ``read_write`` one.

Requires the packages in python/requirements.txt; ``./install.sh`` puts them in
python/.venv.
"""

import argparse
import os
import shutil
import sys
import time
from decimal import Decimal

try:
    import requests
except ModuleNotFoundError as error:
    # ./verify runs on curl and openssl, so setup can look complete while the
    # virtualenv is not active - most often in a terminal opened later.
    raise SystemExit(
        f"{error.name} is not installed. Activate the virtualenv ./install.sh made:\n"
        f"  source python/.venv/bin/activate"
    ) from None

# stx.py sits in python/: the host table, the profile loader and the signing
# scheme, shared by every Python example here so that a hostname and a signature
# are each defined once.
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import stx  # noqa: E402


def request(config, private_key, method, path, body=None):
    """Send one signed request and return the decoded JSON.

    The signature covers the method and the path INCLUDING any query string, so
    the path is built before signing and then used verbatim. Signing
    ``/api/v1/markets`` and sending ``/api/v1/markets?status=open`` is a 401.
    """
    headers = stx.signed_headers(private_key, config["key_id"], method, path)
    headers["Content-Type"] = "application/json"

    try:
        response = requests.request(
            method,
            config["base_url"] + path,
            headers=headers,
            json=body,
            timeout=15,
        )
    except requests.exceptions.ConnectionError as error:
        sys.exit(stx.unreachable(config["base_url"], error))

    if response.status_code == 401:
        sys.exit(
            "401 unauthorized.\n"
            "  The signature, key id or timestamp was rejected. The most common causes:\n"
            "  - the key belongs to a different environment than "
            f"{config['exchange']}/{config['environment']}\n"
            "  - the machine clock is more than 30 seconds off\n"
            f"  Body: {response.text[:300]}"
        )

    if not response.ok:
        # 400 is a malformed request. On POST /api/v1/orders the usual cause is
        # a `price` sent as a number instead of a dollar string - see
        # cmd_roundtrip. 422 is the exchange rejecting a well-formed,
        # authenticated request: a price at or above the market's max_price,
        # say. Either message is worth reading in full.
        sys.exit(f"HTTP {response.status_code}: {response.text[:500]}")

    return response.json()


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def cmd_me(config, private_key, _args):
    """GET /api/v1/me - the only place your user id is published.

    Private WebSocket topics are scoped by it: orders:<user_id>,
    balances:<user_id>, account:<user_id>, and so on. Fetch it once at startup
    and hold it.
    """
    me = request(config, private_key, "GET", "/api/v1/me")["me"]
    print(f"user_id     {me['user_id']}")
    print(f"account_id  {me['account_id']}")
    print(f"key_id      {me['key_id']}")
    print(f"scope       {me['scope']}")
    if me["scope"] == "read_only":
        print("\nThis key cannot place or cancel orders. `roundtrip` needs read_write.")


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------


# The endpoint's ceiling: limit=300 and limit=1000 both come back with 200.
# Omitting limit entirely gives you 100.
MARKET_PAGE_LIMIT = 200


def list_markets(config, private_key, limit=MARKET_PAGE_LIMIT):
    """One page of markets.

    Collections come back as {"cursor": ..., "markets": [...]}, not {"data": ...}.
    Pass the cursor back as ?cursor=... for the next page; it is null on the last.

    `status` is lowercase: ?status=OPEN is a 400, and `open` is the only value
    the endpoint accepts. `trading=true` works as a query param too, though the
    examples filter on the `trading` field below so the raw page stays visible.
    """
    path = f"/api/v1/markets?status=open&limit={limit}"
    return request(config, private_key, "GET", path)["markets"]


def tradeable_markets(markets):
    """Markets that are actually quotable right now, best book first."""
    live = [m for m in markets if m.get("trading") and m.get("status") == "open"]
    return sorted(live, key=lambda m: len(m.get("bids") or []) + len(m.get("offers") or []),
                  reverse=True)


def cmd_markets(config, private_key, _args):
    open_markets = list_markets(config, private_key)
    markets = tradeable_markets(open_markets)
    if not markets:
        sys.exit("No tradeable markets right now.")

    # Every price here is a dollar string ("0.6100", "1.0000"), so the column
    # is a straight format rather than a conversion. A US market settles at $1,
    # so max_price is "1.0000" and quotes run $0.01-$0.99. Canada settles at
    # $100 and max_price is "100.0000". Read it off the market, do not assume.
    # Symbols are back-loaded: the leg that distinguishes sibling markets
    # (TOTAL-3_5 from TOTAL-4_5) is in the tail, and --market takes a symbol,
    # so this column has to survive intact. TITLE is last and absorbs the
    # slack - a narrow terminal costs you title text, never the identifier.
    shown = markets[:15]
    symbol_width = max(len(m["symbol"]) for m in shown)
    title_width = max(20, shutil.get_terminal_size().columns - symbol_width - 26)

    print(f"{'SYMBOL':<{symbol_width}} {'BID':>7} {'OFFER':>7} {'MAX':>7}  TITLE")
    for market in shown:
        bids = market.get("bids") or []
        offers = market.get("offers") or []
        bid = stx.fmt_money(bids[0]["price"]) if bids else "-"
        offer = stx.fmt_money(offers[0]["price"]) if offers else "-"
        print(
            f"{market['symbol']:<{symbol_width}} {bid:>7} {offer:>7} "
            f"{stx.fmt_money(market['max_price']):>7}  {(market.get('title') or '')[:title_width]}"
        )

    print(f"\n{len(markets)} tradeable of {len(open_markets)} returned by "
          f"?status=open&limit={MARKET_PAGE_LIMIT}.")


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


def cmd_orders(config, private_key, _args):
    orders = request(config, private_key, "GET", "/api/v1/orders?status=open")["orders"]
    if not orders:
        print("No open orders.")
        return

    # price is a dollar string, or null on a market order. quantity and filled
    # are strings too ("1.00"), and print as they arrive - a column needs no
    # conversion. Parse before doing ARITHMETIC on them, as cmd_roundtrip does.
    print(f"{'ID':<38} {'SIDE':<5} {'QTY':>6} {'PRICE':>7} {'FILLED':>7}  STATUS")
    for order in orders:
        price = stx.fmt_money(order["price"]) if order.get("price") is not None else "MKT"
        print(
            f"{order['id']:<38} {order['action']:<5} {order['quantity']:>6} "
            f"{price:>7} {order.get('filled', '0.00'):>7}  {order['status']}"
        )


def cmd_roundtrip(config, private_key, args):
    """Place a limit order well away from the touch, then cancel it.

    Priced so it should rest rather than fill, but this is a real order on a
    real book: on integration that costs nothing, on production it does not.
    """
    if config["environment"] == "production" and not args.force_production:
        sys.exit(
            "Refusing to place orders against production from an example script.\n"
            f"Profile [{config['profile']}] points at {config['base_url']}.\n"
            "Pass --force-production if you really mean to."
        )

    candidates = [m for m in tradeable_markets(list_markets(config, private_key))
                  if m.get("bids")]
    if not candidates:
        sys.exit("No tradeable market with a bid to price against.")

    market = candidates[0]
    # Decimal, not float: these are exact decimal values and float64 is not.
    best_bid = stx.to_decimal(market["bids"][0]["price"])

    # Ten cents under the touch, floored at a cent. The ceiling is the market's
    # own max_price, and going over it is a 422 quoting the cap.
    price = max(Decimal("0.01"), best_bid - Decimal("0.10"))

    print(f"{market['symbol']}  best bid {stx.fmt_money(best_bid)}, "
          f"max_price {stx.fmt_money(market['max_price'])}")
    print(f"placing    BUY 1 @ {stx.fmt_money(price)}")

    body = {
        "market_id": market["market_id"],
        "order_type": "limit",
        "action": "buy",
        # A STRING, in dollars. Sending the number 51 is a 400: an integer used
        # to mean 51 cents, and reading it as $51.00 would be a 100x overprice,
        # so the server rejects it rather than guessing. quantity is exempt -
        # a contract count carries no unit ambiguity - and still takes a number.
        "price": stx.dollar_string(price),
        "quantity": 1,
        # Your own reference, echoed back on the order and on every socket
        # push about it. Use it to tie exchange state to your own.
        "client_order_id": f"quickstart-{int(time.time())}",
    }

    # The body is flat. Wrapping it in {"user_order": {...}} returns 400. And a
    # successful placement is a 200, not a 201.
    order = request(config, private_key, "POST", "/api/v1/orders", body)["order"]
    # The echoed price is the same order at the server's width: "0.51" in,
    # "0.5100" back. Compare as decimals, never as strings.
    print(f"placed     {order['id']}  status={order['status']}  "
          f"price={order['price']}  filled={order.get('filled', '0.00')}")

    cancelled = request(
        config, private_key, "DELETE", f"/api/v1/orders/{order['id']}"
    )
    print(f"cancelled  status={cancelled['status']}")


COMMANDS = {
    "me": cmd_me,
    "markets": cmd_markets,
    "orders": cmd_orders,
    "roundtrip": cmd_roundtrip,
}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", nargs="?", default="me", choices=sorted(COMMANDS))
    parser.add_argument("--profile", help="profile in ~/.stx/credentials")
    parser.add_argument("--force-production", action="store_true",
                        help="allow `roundtrip` to place a real order on a production profile")
    args = parser.parse_args()

    config = stx.load_profile(args.profile)
    private_key = stx.load_private_key(config)
    print(f"[{config['profile']} -> {config['base_url']}]\n", file=sys.stderr)

    COMMANDS[args.command](config, private_key, args)


if __name__ == "__main__":
    main()
