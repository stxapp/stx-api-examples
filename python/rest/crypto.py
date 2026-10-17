#!/usr/bin/env python3
"""STX crypto markets - find the live BTC and ETH windows, read the book, quote both sides.

    python python/rest/crypto.py windows                  # the open window and the next ones
    python python/rest/crypto.py windows --coin eth
    python python/rest/crypto.py book --coin btc --side up
    python python/rest/crypto.py quote --coin btc --side up --bid 0.45 --offer 0.55
    python python/rest/crypto.py quote --coin btc --side up --bid 0.45 --offer 0.55 --keep
    python python/rest/crypto.py results --coin btc       # how recent windows settled

Each crypto market is one short window, 15 minutes today, on one coin. A window
has two markets: "Up" settles at $1 if the coin's price at the end of the
window is at or above its price at the start, "Down" settles at $1 if it is
not. The market's `question` and `description` state the exact reference price
and times. A new window opens as the last one closes, so a client looks the
current markets up again for every window rather than holding on to a market id.

A window moves through these statuses:

    scheduled  listed ahead of time, not tradeable yet
    open       trading for the length of the window
    closed     trading has stopped; `result` turns "won" or "lost"
    resulted   settled

Crypto markets are listed under the sport "Crypto", so ``?sports=Crypto`` on
GET /api/v1/markets returns them and nothing else. Every market's `max_price`
is read off the market, as in quickstart.py: here it is "1.0000".

``quote`` places a limit buy at --bid and a limit sell at --offer on the
current window, shows them on the book, and cancels both unless --keep is
given. A sell needs no position: you go short, and your account must cover the
risk ($1 minus the price, per contract). It needs a read_write key and refuses
a production profile.

Credentials come from ~/.stx/credentials, written by ``./configure``. Add
``--profile <name>`` to use one other than ``[default]``.

Requires the packages in python/requirements.txt; ``./install.sh`` puts them in
python/.venv.
"""

import argparse
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

try:
    import requests
except ModuleNotFoundError as error:
    raise SystemExit(
        f"{error.name} is not installed. Activate the virtualenv ./install.sh made:\n"
        f"  source python/.venv/bin/activate"
    ) from None

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import stx  # noqa: E402

COINS = ("btc", "eth")
SIDES = ("up", "down")


def request(config, private_key, method, path, body=None):
    """Send one signed request and return the decoded JSON.

    The signature covers the path including its query string, so the path is
    sent exactly as signed. That is why the sort below is written
    ``sort_by%5Bname%5D`` rather than ``sort_by[name]``: an HTTP library may
    re-encode the brackets after signing, and then the signature no longer
    matches and the request is a 401.
    """
    headers = stx.signed_headers(private_key, config["key_id"], method, path)
    headers["Content-Type"] = "application/json"
    try:
        response = requests.request(method, config["base_url"] + path,
                                    headers=headers, json=body, timeout=15)
    except requests.exceptions.ConnectionError as error:
        sys.exit(stx.unreachable(config["base_url"], error))

    if response.status_code == 401:
        sys.exit(
            "401 unauthorized: the signature, key id or timestamp was rejected.\n"
            f"  Is this key for {config['region']}/{config['env']}? Is the clock right?\n"
            f"  Body: {response.text[:300]}"
        )
    if not response.ok:
        sys.exit(f"HTTP {response.status_code} on {method} {path}: {response.text[:500]}")
    return response.json()


# ---------------------------------------------------------------------------
# Finding the markets
# ---------------------------------------------------------------------------


def crypto_markets(config, private_key, statuses, limit=100, newest_first=False):
    """Crypto markets in the given statuses, following the cursor to the end."""
    query = f"sports=Crypto&status={','.join(statuses)}&limit={limit}"
    if newest_first:
        query += "&sort_by%5Bname%5D=event_start&sort_by%5Bdirection%5D=desc"

    markets, path = [], f"/api/v1/markets?{query}"
    while True:
        page = request(config, private_key, "GET", path)
        markets.extend(page["markets"])
        # One page is enough when the caller only wants the newest few.
        if not page["cursor"] or newest_first:
            return markets
        path = (f"/api/v1/markets?{query}"
                f"&cursor={requests.utils.quote(page['cursor'], safe='')}")


def coin_of(market):
    """'btc' or 'eth'. The market's `specifier` carries the coin."""
    return (market.get("specifier") or "").lower()


def side_of(market):
    """'up' or 'down', from the market's `rules` ("crypto_up" / "crypto_down")."""
    return (market.get("rules") or "").removeprefix("crypto_")


def window_of(market):
    """(start, end) of the market's window, as UTC datetimes.

    `event_start` is the start. The window's length is the MIN<n> at the end of
    the symbol (STXCRYPTO-26OCT162330-UPBTCMIN15 is a 15-minute window); the
    `question` states the same end time in words.
    """
    start = datetime.fromisoformat(market["event_start"].replace("Z", "+00:00"))
    match = re.search(r"MIN(\d+)$", market["symbol"])
    minutes = int(match.group(1)) if match else 15
    return start, start + timedelta(minutes=minutes)


def pick(markets, coin, side):
    """The market for one coin and side, earliest window first."""
    found = sorted((m for m in markets if coin_of(m) == coin and side_of(m) == side),
                   key=lambda m: m["event_start"])
    return found[0] if found else None


def current_market(config, private_key, coin, side):
    market = pick(crypto_markets(config, private_key, ["open"]), coin, side)
    if not market:
        sys.exit(f"No open {coin.upper()} {side} window right now. "
                 "`windows` shows when the next one opens.")
    return market


def fmt_time(moment):
    return moment.astimezone().strftime("%H:%M:%S %Z")


def fmt_left(moment):
    seconds = int((moment - datetime.now(timezone.utc)).total_seconds())
    if seconds <= 0:
        return "ended"
    return f"{seconds // 60}m{seconds % 60:02d}s"


def best(levels):
    return stx.fmt_money(levels[0]["price"]) if levels else "-"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_windows(config, private_key, args):
    """The open window and the scheduled ones after it, one row per market."""
    markets = crypto_markets(config, private_key, ["open", "scheduled"])
    markets = [m for m in markets if not args.coin or coin_of(m) == args.coin]
    if not markets:
        sys.exit("No open or scheduled crypto markets.")

    markets.sort(key=lambda m: (m["event_start"], coin_of(m), side_of(m) != "up"))
    symbol_width = max(len(m["symbol"]) for m in markets)
    print(f"{'SYMBOL':<{symbol_width}} {'STATUS':<9} {'START':>12} {'END':>12} "
          f"{'LEFT':>7} {'BID':>6} {'OFFER':>6}")
    for market in markets:
        start, end = window_of(market)
        left = fmt_left(end) if market["status"] == "open" else ""
        print(f"{market['symbol']:<{symbol_width}} {market['status']:<9} "
              f"{fmt_time(start):>12} {fmt_time(end):>12} {left:>7} "
              f"{best(market.get('bids') or []):>6} {best(market.get('offers') or []):>6}")

    open_now = [m for m in markets if m["status"] == "open"]
    if open_now:
        print(f"\n{open_now[0]['question']}")
        print(open_now[0]["description"])


def cmd_book(config, private_key, args):
    """Every resting level on the current window for one coin and side.

    The book comes back on the market itself as `bids` and `offers`, best
    first, each level a dollar `price` and a `quantity` string. For a book that
    updates as it changes, join the market's channel instead:
    python python/websockets/watch.py --market <symbol>
    """
    market = current_market(config, private_key, args.coin, args.side)
    _, end = window_of(market)
    print(f"{market['symbol']}  closes {fmt_time(end)} ({fmt_left(end)} left)")
    print(f"{market['question']}\n")

    bids, offers = market.get("bids") or [], market.get("offers") or []
    if not bids and not offers:
        print("The book is empty.")
        return
    print(f"{'BID QTY':>10} {'BID':>7}   {'OFFER':<7} {'OFFER QTY':<10}")
    for i in range(max(len(bids), len(offers))):
        bid = bids[i] if i < len(bids) else None
        offer = offers[i] if i < len(offers) else None
        print(f"{bid['quantity'] if bid else '':>10} "
              f"{stx.fmt_money(bid['price']) if bid else '':>7}   "
              f"{stx.fmt_money(offer['price']) if offer else '':<7} "
              f"{offer['quantity'] if offer else '':<10}")


def place(config, private_key, market, action, price, quantity):
    body = {
        "market_id": market["market_id"],
        "order_type": "limit",
        "action": action,
        # Price and quantity are dollar STRINGS. A JSON number is a 400.
        "price": stx.dollar_string(price),
        "quantity": quantity,
        "client_order_id": f"crypto-{action}-{int(time.time() * 1000)}",
    }
    order = request(config, private_key, "POST", "/api/v1/orders", body)["order"]
    print(f"placed     {action.upper():<4} {quantity} @ {stx.fmt_money(price)}  "
          f"{order['id']}  status={order['status']}")
    return order


def show_resting(config, private_key, market, orders):
    """Read the market back until our orders are on the book, or give up.

    A new order answers `accepted` and reaches the book a moment later, so the
    first read straight after placing can miss it.
    """
    want = {order["action"]: Decimal(order["price"]) for order in orders}
    for _ in range(10):
        current = request(config, private_key, "GET",
                          f"/api/v1/markets?market_ids={market['market_id']}")["markets"][0]
        bids = [Decimal(level["price"]) for level in current.get("bids") or []]
        offers = [Decimal(level["price"]) for level in current.get("offers") or []]
        seen = (("buy" not in want or want["buy"] in bids)
                and ("sell" not in want or want["sell"] in offers))
        if seen:
            print(f"on book    best bid {best(current.get('bids') or [])}  "
                  f"best offer {best(current.get('offers') or [])}")
            return
        time.sleep(0.3)
    print("on book    not seen yet; `python python/rest/quickstart.py orders` lists them")


def cmd_quote(config, private_key, args):
    """A two-sided quote on the current window: buy at --bid, sell at --offer."""
    if config["env"] == "prod" and not args.force_production:
        sys.exit(
            "Refusing to place orders against production from an example script.\n"
            f"Profile [{config['profile']}] points at {config['base_url']}.\n"
            "Pass --force-production if you really mean to."
        )

    bid, offer = Decimal(args.bid), Decimal(args.offer)
    market = current_market(config, private_key, args.coin, args.side)
    max_price = stx.to_decimal(market["max_price"])
    # Prices run from one cent to one cent under max_price. Anything at or over
    # max_price is a 422 from the exchange.
    if not (Decimal("0.01") <= bid < offer < max_price):
        sys.exit(f"Need 0.01 <= bid < offer < {stx.fmt_money(max_price)}.")

    _, end = window_of(market)
    print(f"{market['symbol']}  closes {fmt_time(end)} ({fmt_left(end)} left)")
    print(f"book       best bid {best(market.get('bids') or [])}  "
          f"best offer {best(market.get('offers') or [])}")

    # A bid at or above the best offer (or an offer at or below the best bid)
    # trades straight away, unless that resting order is your own: crossing
    # yourself is rejected with rejection_reason "match_with_self".
    orders = [place(config, private_key, market, "buy", bid, args.quantity),
              place(config, private_key, market, "sell", offer, args.quantity)]
    show_resting(config, private_key, market, orders)

    if args.keep:
        print("\nLeft on the book. Cancel each with DELETE /api/v1/orders/<id>.")
        return
    for order in orders:
        cancelled = request(config, private_key, "DELETE", f"/api/v1/orders/{order['id']}")
        print(f"cancelled  {order['id']}  status={cancelled['status']}")


def cmd_results(config, private_key, args):
    """How the most recent windows settled, newest first.

    In each window one market is "won" and the other "lost". `result` is
    "pending" until the end price is in.
    """
    markets = crypto_markets(config, private_key, ["closed", "resulted"],
                             limit=args.limit * 4, newest_first=True)
    markets = [m for m in markets if not args.coin or coin_of(m) == args.coin]
    if not markets:
        sys.exit("No settled crypto windows yet.")

    symbol_width = max(len(m["symbol"]) for m in markets)
    print(f"{'SYMBOL':<{symbol_width}} {'STATUS':<9} {'END':>12} {'RESULT':<8} VOLUME")
    # Two markets per coin per window: Up and Down.
    per_window = 2 if args.coin else 2 * len(COINS)
    for market in markets[: args.limit * per_window]:
        _, end = window_of(market)
        print(f"{market['symbol']:<{symbol_width}} {market['status']:<9} "
              f"{fmt_time(end):>12} {market['result']:<8} {market.get('total_volume') or '0.00'}")


COMMANDS = {"windows": cmd_windows, "book": cmd_book, "quote": cmd_quote, "results": cmd_results}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", nargs="?", default="windows", choices=list(COMMANDS))
    parser.add_argument("--profile", help="profile in ~/.stx/credentials")
    parser.add_argument("--coin", choices=COINS, help="btc or eth (book/quote default: btc)")
    parser.add_argument("--side", choices=SIDES, default="up", help="the Up or the Down market")
    parser.add_argument("--bid", default="0.45", help="`quote`: buy price in dollars")
    parser.add_argument("--offer", default="0.55", help="`quote`: sell price in dollars")
    parser.add_argument("--quantity", default="1", help="`quote`: contracts per side")
    parser.add_argument("--keep", action="store_true", help="`quote`: leave the orders resting")
    parser.add_argument("--limit", type=int, default=4, help="`results`: how many windows to show")
    parser.add_argument("--force-production", action="store_true",
                        help="allow `quote` to place real orders on a production profile")
    args = parser.parse_args()
    if args.command in ("book", "quote") and not args.coin:
        args.coin = "btc"

    config = stx.load_profile(args.profile)
    private_key = stx.load_private_key(config)
    print(f"[{config['profile']} -> {config['base_url']}]\n", file=sys.stderr)

    COMMANDS[args.command](config, private_key, args)


if __name__ == "__main__":
    main()
