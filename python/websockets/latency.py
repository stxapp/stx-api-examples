#!/usr/bin/env python3
"""Measure how long it takes to place an order and see it on the book.

    python python/websockets/latency.py
    python python/websockets/latency.py --rounds 10 --market <market id or symbol>

Picks a market with a resting book, subscribes to the public `orderbook` topic
for it,
then repeats a round --rounds times (5 by default, capped at 10). Each round
has two legs:

    place    POST   /api/v1/orders        rest a buy limit 10c under the touch
                                          (`price` is a dollar string: "0.51")
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
from decimal import Decimal

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
    try:
        response = requests.request(
            method, config["base_url"] + path, headers=headers, json=body, timeout=15
        )
    except requests.exceptions.ConnectionError as error:
        raise SystemExit(stx.unreachable(config["base_url"], error)) from None
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
    """Wait for the next `book` frame, ignoring everything else.

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
            raise TimeoutError(f"no book update within {timeout}s")
        frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=remaining))
        if frame[3] == "book":
            return frame[4]


# `unmatched topic` is the server saying it has never heard of the topic, which
# on a correct client means the host predates it. Worth naming, because it looks
# identical to a typo and is the single most likely failure while the
# dollar-format topics are still rolling out.
UNMATCHED_TOPIC_HINT = (
    "  'unmatched topic' means the server does not know this topic.\n"
    "  The dollar-format topics (orderbook, ticker, trades, orders:, fills:,\n"
    "  positions:, settlements:, balances:, account:) need a host running them;\n"
    "  older deployments carry only the legacy cents topics. See CHANNELS.md."
)


def join_error(topic, response):
    message = f"join rejected for topic {topic!r}: {response}"
    if isinstance(response, dict) and response.get("reason") == "unmatched topic":
        message += "\n" + UNMATCHED_TOPIC_HINT
    return RuntimeError(message)


async def join_reply(ws, topic, timeout=BOOK_TIMEOUT_SECONDS):
    """Wait for the reply to our phx_join, and settle before timing anything.

    The reply carries only `selected_market_ids`, not a book - `orderbook`
    sends no snapshot on join. This is purely a settle: it confirms the join
    landed. Waiting for a `book` push instead would block for the whole timeout
    on any market that happens to be quiet, which is most of them, most of the
    time.

    A rejected join replies with status "error", not a closed socket. Checking
    it here turns a bad market_ids payload into one clear message instead of a
    BOOK_TIMEOUT_SECONDS wait for a push that was never going to arrive.
    """
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"no join reply within {timeout}s")
        frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=remaining))
        if frame[3] == "phx_reply":
            reply = frame[4]
            if reply.get("status") != "ok":
                raise join_error(topic, reply.get("response") or reply)
            return reply


async def heartbeat(ws, interval=20):
    ref = 9000
    while True:
        await asyncio.sleep(interval)
        ref += 1
        await ws.send(json.dumps([None, str(ref), "phoenix", "heartbeat", {}]))


async def run(config, private_key, market, rounds):
    headers = stx.signed_headers(private_key, config["key_id"], "GET", stx.SOCKET_PATH)
    # One public topic for every market, narrowed by the join payload. At least
    # one valid market_id is required: an empty list is a join error, not a
    # subscription to everything.
    topic = "orderbook"
    join_payload = {"market_ids": [market["market_id"]]}

    # REST money is a dollar string. Decimal, not float: these are exact
    # decimal values and float64 is not.
    best_bid = stx.to_decimal(market["bids"][0]["price"])
    # Well under the touch so it rests rather than fills. A fill would measure
    # the matching engine instead of the book publish, and would leave a
    # position behind.
    price = max(Decimal("0.01"), best_bid - Decimal("0.10"))

    print(f"{market['symbol']}  best bid {stx.fmt_money(best_bid)}, "
          f"quoting {stx.fmt_money(price)}, {rounds} round(s)")
    print(f"{'ROUND':<7} {'LEG':<8} {'REST':>9} {'WS':>9} {'TOTAL':>9}")

    samples = []

    async with websockets.connect(
        f"{config['socket_url']}?vsn=2.0.0", additional_headers=headers
    ) as ws:
        keepalive = asyncio.create_task(heartbeat(ws))
        try:
            await ws.send(json.dumps(["1", "1", topic, "phx_join", join_payload]))
            await join_reply(ws, topic)    # settle: drain the join reply

            order = None
            try:
                for round_number, leg in rounds_and_legs(rounds):
                    started = time.perf_counter()

                    if leg == "place":
                        body = {
                            "market_id": market["market_id"],
                            "order_type": "limit",
                            "action": "buy",
                            # A string, in dollars. The number 51 is a 400 -
                            # see javascript/stx.mjs or python/stx.py.
                            "price": stx.dollar_string(price),
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
    except (RuntimeError, TimeoutError) as error:
        # A rejected join or a book that never arrives. Both are ordinary
        # operational failures, not bugs, so report them as one line rather
        # than a traceback.
        sys.exit(str(error))


if __name__ == "__main__":
    main()
