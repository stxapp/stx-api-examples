#!/usr/bin/env python3
"""Measure how long it takes to place an order and see it on the book.

    python python/websockets/latency.py
    python python/websockets/latency.py --rounds 10 --market <market id or symbol>

Picks a market with a resting book, subscribes to that market's socket topic,
then repeats a round --rounds times (5 by default, capped at 10). Each round
has two legs:

    place    POST   /api/v1/orders        rest a buy limit 10c under the touch
    cancel   DELETE /api/v1/orders/<id>   take that same order back off

Both legs are timed the same way - fire the HTTP call, then wait for the order
book push that reflects it - and print one row each:

    ROUND   which round, 1..--rounds
    LEG     place or cancel, as above
    REST    the HTTP round trip - request out, response in
    WS      from that response to the socket push that shows the change
    TOTAL   REST + WS, which is what your quoting loop actually sees

The summary at the end averages all three columns over every leg, place and
cancel together, and adds min, median and max of TOTAL.

The order rests 10c below the best bid rather than crossing it: a fill would
measure the matching engine instead of the book publish, and would leave a
position behind. Every round cancels what it placed, so nothing is left open.

javascript/websockets/latency.mjs is the same measurement in Node, so the two
can be compared directly: same exchange, same market, different runtime and
different WebSocket library.

This places REAL orders. It refuses to run against a production profile.

Requires the packages in python/requirements.txt; ``./install.sh`` puts them in
python/.venv.
"""

import argparse
import asyncio
import json
import os
import statistics
import sys
import time

try:
    import requests
    import websockets
except ModuleNotFoundError as error:
    # ./verify runs on curl and openssl, so setup can look complete while the
    # virtualenv is not active - most often in a terminal opened later.
    raise SystemExit(
        f"{error.name} is not installed. Activate the virtualenv ./install.sh made:\n"
        f"  source python/.venv/bin/activate"
    ) from None

# stx.py sits in python/. Appended rather than prepended so this directory
# (python/websockets/) cannot shadow the installed `websockets` package.
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import stx  # noqa: E402

BOOK_TIMEOUT_SECONDS = 15

# Every round puts a real order on a real book and takes it off again. This is a
# measurement, not a load test, and 10 rounds is already 20 legs - well past the
# point where more samples tell you anything new about your own network path.
MAX_ROUNDS = 10
DEFAULT_ROUNDS = 5

# Printed on every run, to stderr alongside the profile line, so that the table
# below explains itself to someone who never opens this file. stderr keeps it
# out of the way when the numbers are piped somewhere.
BANNER = """
What this measures: how long from sending an order to seeing it on the book.
Each round has two legs, both timed the same way - fire the HTTP call, then
wait for the order book push that reflects it.

  place    POST   /api/v1/orders        buy limit, 10c under the best bid
  cancel   DELETE /api/v1/orders/<id>   that same order, taken back off

  REST     the HTTP round trip - request out, response in
  WS       from that response to the socket push that shows the change
  TOTAL    REST + WS - what your quoting loop actually sees

These are REAL orders. They rest below the touch so they do not fill, and every
round cancels what it placed.
"""


def rounds_and_legs(rounds):
    """(round number, leg) for every measurement, in order."""
    for round_number in range(1, rounds + 1):
        for leg in ("place", "cancel"):
            yield round_number, leg


def rest(config, private_key, method, path, body=None):
    headers = stx.signed_headers(private_key, config["key_id"], method, path)
    headers["Content-Type"] = "application/json"
    response = requests.request(
        method, config["base_url"] + path, headers=headers, json=body, timeout=15
    )
    if not response.ok:
        raise RuntimeError(f"{method} {path} -> HTTP {response.status_code}: {response.text[:300]}")
    return response.json()


def pick_market(config, private_key, market_id=None):
    markets = rest(config, private_key, "GET", "/api/v1/markets?status=open&limit=200")["markets"]
    if market_id:
        for market in markets:
            if market_id in (market["market_id"], market["symbol"]):
                return market
        raise SystemExit(f"Market {market_id} is not open and tradeable right now.")

    live = [m for m in markets if m.get("trading") and m.get("status") == "open" and m.get("bids")]
    if not live:
        raise SystemExit("No tradeable market with a bid to price against.")
    return max(live, key=lambda m: len(m["bids"]) + len(m.get("offers") or []))


async def next_book_update(ws, timeout=BOOK_TIMEOUT_SECONDS):
    """Wait for the next order_book_update frame, ignoring everything else.

    The book publishes on the server's own cadence - roughly every 200 ms - and
    coalesces changes in between. So the WS figure below is dominated by where
    in that window the order landed, not by network time. It is the number that
    matters for a quoting loop even so: it is how long until you can see your
    own order on the book you are quoting from.
    """
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"no order_book_update within {timeout}s")
        frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=remaining))
        if frame[3] == "order_book_update":
            return frame[4]


async def join_reply(ws, timeout=BOOK_TIMEOUT_SECONDS):
    """Wait for the reply to our phx_join, which carries the opening book.

    The book snapshot comes back inside this reply. `order_book_update` fires
    only when the book CHANGES, so waiting for one of those here would block
    for the whole timeout on any market that happens to be quiet - which is
    most of them, most of the time.
    """
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"no join reply within {timeout}s")
        frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=remaining))
        if frame[3] == "phx_reply":
            return frame[4]


async def heartbeat(ws, interval=20):
    ref = 9000
    while True:
        await asyncio.sleep(interval)
        ref += 1
        await ws.send(json.dumps([None, str(ref), "phoenix", "heartbeat", {}]))


async def run(config, private_key, market, rounds):
    headers = stx.signed_headers(private_key, config["key_id"], "GET", stx.SOCKET_PATH)
    topic = f"market:{market['market_id']}"

    # The book quotes dollars, orders take cents - see stx.book_price_cents.
    best_bid = stx.book_price_cents(market["bids"][0]["price"])
    # Well under the touch so it rests rather than fills. A fill would measure
    # the matching engine instead of the book publish, and would leave a
    # position behind.
    price = max(1, best_bid - 10)

    print(f"{market['symbol']}  best bid {best_bid}c, quoting {price}c, {rounds} round(s)")
    print(f"{'ROUND':<7} {'LEG':<8} {'REST':>9} {'WS':>9} {'TOTAL':>9}")

    samples = []

    async with websockets.connect(
        f"{config['socket_url']}?vsn=2.0.0", additional_headers=headers
    ) as ws:
        keepalive = asyncio.create_task(heartbeat(ws))
        try:
            await ws.send(json.dumps(["1", "1", topic, "phx_join", {}]))
            await join_reply(ws)           # settle: drain the join reply

            order = None
            try:
                for round_number, leg in rounds_and_legs(rounds):
                    started = time.perf_counter()

                    if leg == "place":
                        body = {
                            "market_id": market["market_id"],
                            "order_type": "limit",
                            "action": "buy",
                            "price": price,
                            "quantity": 1,
                            "client_order_id": f"latency-{int(time.time() * 1000)}",
                        }
                        order = rest(config, private_key, "POST", "/api/v1/orders", body)["order"]
                    else:
                        rest(config, private_key, "DELETE", f"/api/v1/orders/{order['id']}")

                    responded = time.perf_counter()
                    await next_book_update(ws)
                    seen = time.perf_counter()

                    rest_ms = (responded - started) * 1000
                    ws_ms = (seen - responded) * 1000
                    total_ms = (seen - started) * 1000
                    samples.append((leg, rest_ms, ws_ms, total_ms))

                    print(f"{round_number:<7} {leg:<8} {rest_ms:>8.1f}ms "
                          f"{ws_ms:>8.1f}ms {total_ms:>8.1f}ms")
            except (RuntimeError, TimeoutError) as error:
                # Report what was measured before the failure rather than
                # discarding it.
                print(f"\nstopped early: {error}", file=sys.stderr)
        finally:
            keepalive.cancel()

    if not samples:
        return

    totals = sorted(sample[3] for sample in samples)
    print()
    print(f"{len(samples)} samples")
    print(f"  REST  mean {statistics.mean(s[1] for s in samples):7.1f}ms")
    print(f"  WS    mean {statistics.mean(s[2] for s in samples):7.1f}ms")
    print(f"  TOTAL mean {statistics.mean(totals):7.1f}ms   "
          f"min {totals[0]:.1f}  p50 {statistics.median(totals):.1f}  max {totals[-1]:.1f}")


def main():
    parser = argparse.ArgumentParser(description="Measure place -> book update latency.")
    parser.add_argument("--profile", help="profile in ~/.stx/credentials")
    parser.add_argument("--market", help="market id or symbol (default: deepest book)")
    parser.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS,
                        help=f"place/cancel pairs (default {DEFAULT_ROUNDS}, "
                             f"capped at {MAX_ROUNDS})")
    parser.add_argument("--force-production", action="store_true",
                        help="allow a production profile to place real orders")
    args = parser.parse_args()

    config = stx.load_profile(args.profile)

    # This places real orders. Against production that is real money on a real
    # book, which nobody means to do from a measurement script by accident.
    if config["environment"] == "production" and not args.force_production:
        sys.exit(
            f"Refusing to place orders against production.\n"
            f"Profile [{config['profile']}] points at {config['base_url']}.\n"
            f"Run this against an integration profile, or pass --force-production if\n"
            f"you really mean to put real orders on a real book."
        )

    private_key = stx.load_private_key(config)
    print(f"[{config['profile']} -> {config['base_url']}]", file=sys.stderr)
    print(BANNER, file=sys.stderr)

    # Printed here, after the banner, so it lands directly above the "N round(s)"
    # line it explains. Above the banner it scrolls out of view.
    if args.rounds < 1:
        # Zero or negative would run no legs at all and exit silently, which
        # reads as a hang rather than as bad input.
        print(f"NOTE: --rounds {args.rounds} is not a number of rounds; "
              f"using {DEFAULT_ROUNDS}.\n", file=sys.stderr)
        args.rounds = DEFAULT_ROUNDS
    elif args.rounds > MAX_ROUNDS:
        print(f"NOTE: --rounds {args.rounds} capped at {MAX_ROUNDS}. Each round "
              f"places and cancels a real order on a real book.\n", file=sys.stderr)
        args.rounds = MAX_ROUNDS

    try:
        market = pick_market(config, private_key, args.market)
    except RuntimeError as error:
        sys.exit(str(error))

    try:
        asyncio.run(run(config, private_key, market, args.rounds))
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
