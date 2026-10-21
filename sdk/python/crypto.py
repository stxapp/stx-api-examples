#!/usr/bin/env python3
"""STX crypto markets with the stx-python SDK: list the windows, read a book, quote both sides.

    python sdk/python/crypto.py windows                  # the open window and the next ones
    python sdk/python/crypto.py windows --coin eth
    python sdk/python/crypto.py book --coin btc --side up
    python sdk/python/crypto.py quote --coin btc --side up --bid 0.45 --offer 0.55
    python sdk/python/crypto.py quote --coin btc --side up --bid 0.45 --offer 0.55 --keep
    python sdk/python/crypto.py results --coin btc       # how recent windows settled

Each crypto market is one short window, 15 minutes today, on one coin. A window
has two markets: "Up" settles at $1 if the coin's price at the end of the
window is at or above its price at the start, "Down" settles at $1 if it is
not. A new window opens as the last one closes, so look the markets up again
each window instead of holding on to a market id.

The SDK reads the same ~/.stx/credentials that ./configure writes. Pick a
profile with STX_PROFILE, for example ``STX_PROFILE=us-demo``.

``quote`` places a limit buy at --bid and a limit sell at --offer on the
current window, shows them on the book, and cancels both unless --keep is
given. A sell needs no position: it opens a short. It needs a read_write key
and refuses any exchange but a demo one unless --force-production is given.

Install: pip install -r sdk/python/requirements.txt
"""

import argparse
import re
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List, Optional, Tuple
from urllib.parse import urlparse

from stx import STX, STXRejectedException, STXValidationException
from stx.models import Market

COINS = ("btc", "eth")
SIDES = ("up", "down")


def coin_of(market: Market) -> str:
    """'btc' or 'eth': the market's `specifier`."""
    return (market.specifier or "").lower()


def side_of(market: Market) -> str:
    """'up' or 'down', from the market's `rules` ("crypto_up" / "crypto_down")."""
    rules = market.rules or ""
    return rules[len("crypto_"):] if rules.startswith("crypto_") else rules


def window_of(market: Market) -> Tuple[datetime, datetime]:
    """(start, end) of the market's window, in UTC.

    `event_start` is the start. The length is the MIN<n> at the end of the
    symbol: STXCRYPTO-26OCT162330-UPBTCMIN15 is a 15-minute window.
    """
    start = datetime.fromisoformat(market.event_start.replace("Z", "+00:00"))
    match = re.search(r"MIN(\d+)$", market.symbol or "")
    return start, start + timedelta(minutes=int(match.group(1)) if match else 15)


def not_ended(market: Market) -> bool:
    """False for a window whose end has passed. A demo exchange can still list
    an old window as open or scheduled after it ended, with no book."""
    return window_of(market)[1] > datetime.now(timezone.utc)


def crypto_markets(client: STX, statuses: List[str]) -> List[Market]:
    # Crypto markets are listed under the sport "Crypto".
    return [m for m in client.iter_markets(sports=["Crypto"], status=statuses) if not_ended(m)]


def sorted_book(market: Market):
    """(bids, offers), each best first. Sort them yourself: the market lists
    bids AND offers highest price first, so the raw offers[0] is the worst."""
    return (sorted(market.bids, key=lambda level: Decimal(level.price), reverse=True),
            sorted(market.offers, key=lambda level: Decimal(level.price)))


def current_market(client: STX, coin: str, side: str) -> Market:
    found = [m for m in crypto_markets(client, ["open"]) if coin_of(m) == coin and side_of(m) == side]
    if not found:
        sys.exit(f"No open {coin.upper()} {side} window right now. `windows` shows when the next one opens.")
    return min(found, key=lambda m: m.event_start)


def fmt_time(moment: datetime) -> str:
    return moment.astimezone().strftime("%H:%M:%S %Z")


def fmt_left(end: datetime) -> str:
    seconds = int((end - datetime.now(timezone.utc)).total_seconds())
    return f"{seconds // 60}m{seconds % 60:02d}s" if seconds > 0 else "ended"


def best(levels) -> str:
    """The first level's price. Pass a side from sorted_book, not a raw list."""
    return levels[0].price if levels else "-"


def cmd_windows(client: STX, args) -> int:
    markets = [m for m in crypto_markets(client, ["open", "scheduled"]) if not args.coin or coin_of(m) == args.coin]
    if not markets:
        sys.exit("No open or scheduled crypto markets.")
    markets.sort(key=lambda m: (m.event_start, coin_of(m), side_of(m) != "up"))

    width = max(len(m.symbol) for m in markets)
    print(f"{'SYMBOL':<{width}} {'STATUS':<9} {'START':>12} {'END':>12} {'LEFT':>7} {'BID':>7} {'OFFER':>7}")
    for m in markets:
        start, end = window_of(m)
        left = fmt_left(end) if m.status == "open" else ""
        bids, offers = sorted_book(m)
        print(f"{m.symbol:<{width}} {m.status:<9} {fmt_time(start):>12} {fmt_time(end):>12} "
              f"{left:>7} {best(bids):>7} {best(offers):>7}")

    open_now = next((m for m in markets if m.status == "open"), None)
    if open_now:
        print(f"\n{open_now.question}\n{open_now.description}")
    return 0


def cmd_book(client: STX, args) -> int:
    """Every resting level on the current window. For a book that updates as it
    changes, subscribe to the market over the SDK's WebSocket client instead."""
    market = current_market(client, args.coin, args.side)
    _, end = window_of(market)
    print(f"{market.symbol}  closes {fmt_time(end)} ({fmt_left(end)} left)")
    print(f"{market.question}\n")

    bids, offers = sorted_book(market)
    if not bids and not offers:
        print("The book is empty.")
        return 0
    print(f"{'BID QTY':>10} {'BID':>7}   {'OFFER':<7} {'OFFER QTY':<10}")
    for i in range(max(len(bids), len(offers))):
        bid = bids[i] if i < len(bids) else None
        offer = offers[i] if i < len(offers) else None
        print(f"{bid.quantity if bid else '':>10} {bid.price if bid else '':>7}   "
              f"{offer.price if offer else '':<7} {offer.quantity if offer else '':<10}")
    return 0


def wait_on_book(client: STX, market_id: str, bid: Decimal, offer: Decimal) -> Optional[Market]:
    """Read the market until both prices show. A new order answers `accepted`
    and reaches the book a moment later, so the first read can miss it."""
    for _ in range(10):
        market = client.market(market_id)
        if (bid in {Decimal(level.price) for level in market.bids}
                and offer in {Decimal(level.price) for level in market.offers}):
            return market
        time.sleep(0.3)
    return None


def cmd_quote(client: STX, args) -> int:
    """A two-sided quote on the current window: buy at --bid, sell at --offer."""
    bid, offer = Decimal(args.bid), Decimal(args.offer)
    market = current_market(client, args.coin, args.side)
    max_price = Decimal(market.max_price)
    # Prices run from one cent to one cent under max_price.
    if not (Decimal("0.01") <= bid < offer < max_price):
        sys.exit(f"Need 0.01 <= bid < offer < {market.max_price}.")

    _, end = window_of(market)
    print(f"{market.symbol}  closes {fmt_time(end)} ({fmt_left(end)} left)")
    bids, offers = sorted_book(market)
    print(f"book       best bid {best(bids)}  best offer {best(offers)}")

    placed, exit_code = [], 0
    try:
        for action, price in (("buy", args.bid), ("sell", args.offer)):
            # Price and quantity are dollar strings. The client_order_id is your
            # own id, echoed on the order and on every update about it.
            order = client.place_order(
                market.market_id, action, "limit",
                price=price, quantity=args.quantity, client_order_id=f"crypto-{uuid.uuid4()}",
            )
            placed.append(order.id)
            print(f"placed     {action.upper():<4} {args.quantity} @ {price}  {order.id}  status={order.status}")

        seen = wait_on_book(client, market.market_id, bid, offer)
        if seen:
            bids, offers = sorted_book(seen)
            print(f"on book    best bid {best(bids)}  best offer {best(offers)}")
        else:
            # Crossing your own resting order is rejected with
            # rejection_reason "match_with_self".
            for order_id in placed:
                o = client.order(order_id)
                print(f"not seen   {o.action} @ {o.price}  status={o.status}  {o.rejection_reason or ''}")
            exit_code = 1
    except (STXRejectedException, STXValidationException) as exc:
        print(f"Order refused ({exc.status_code}): {exc}", file=sys.stderr)
        exit_code = 1
    finally:
        if args.keep and exit_code == 0:
            print("\nLeft on the book. Cancel each with client.cancel_order(order_id).")
        else:
            for order_id in placed:
                print(f"cancelled  {order_id}  status={client.cancel_order(order_id).status}")
    return exit_code


def cmd_results(client: STX, args) -> int:
    """How the most recent windows settled, newest first. In each window one
    market is "won" and the other "lost"; `result` is "pending" until then."""
    per_window = 2 if args.coin else 2 * len(COINS)
    page = client.markets(sports=["Crypto"], status=["closed", "resulted"],
                          sort_by="event_start", sort_direction="desc", limit=args.limit * 2 * len(COINS))
    markets = [m for m in page if not args.coin or coin_of(m) == args.coin][: args.limit * per_window]
    if not markets:
        sys.exit("No settled crypto windows yet.")

    width = max(len(m.symbol) for m in markets)
    print(f"{'SYMBOL':<{width}} {'STATUS':<9} {'END':>12} {'RESULT':<8} VOLUME")
    for m in markets:
        _, end = window_of(m)
        print(f"{m.symbol:<{width}} {m.status:<9} {fmt_time(end):>12} {m.result:<8} {m.total_volume or '0.00'}")
    return 0


COMMANDS = {"windows": cmd_windows, "book": cmd_book, "quote": cmd_quote, "results": cmd_results}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", nargs="?", default="windows", choices=list(COMMANDS))
    parser.add_argument("--coin", choices=COINS, help="btc or eth (book/quote default: btc)")
    parser.add_argument("--side", choices=SIDES, default="up", help="the Up or the Down market")
    parser.add_argument("--bid", default="0.45", help="`quote`: buy price in dollars")
    parser.add_argument("--offer", default="0.55", help="`quote`: sell price in dollars")
    parser.add_argument("--quantity", default="1", help="`quote`: contracts per side")
    parser.add_argument("--keep", action="store_true", help="`quote`: leave the orders resting")
    parser.add_argument("--limit", type=int, default=4, help="`results`: how many windows to show")
    parser.add_argument("--force-production", action="store_true",
                        help="allow `quote` to place real orders on a non-demo exchange")
    args = parser.parse_args()
    if args.command in ("book", "quote") and not args.coin:
        args.coin = "btc"

    with STX() as client:
        # Orders go only to a demo exchange or your own machine unless you say otherwise.
        host = urlparse(client.base_url).hostname or ""
        if args.command == "quote" and not args.force_production and not (
                host.startswith("demo.") or host in ("localhost", "127.0.0.1")):
            sys.exit(f"Refusing to place orders on {client.base_url} from an example script.\n"
                     "Pass --force-production if you really mean to.")
        print(f"[{client.base_url}]\n", file=sys.stderr)
        return COMMANDS[args.command](client, args)


if __name__ == "__main__":
    sys.exit(main())
