#!/usr/bin/env python3
"""Measure the place-order to book-update round trip.

    python python/websockets/latency.py
    python python/websockets/latency.py --rounds 10 --market <market_id>

Each round places a resting limit order over REST, waits for the order book
push that reflects it on the socket, then cancels it and waits again. It reports
three numbers per leg:

    REST   the HTTP round trip - request out, response in
    WS     from that response to the socket push that shows the change
    TOTAL  the two together, which is what your quoting loop actually sees

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

import requests
import websockets

# stx.py sits in python/. Appended rather than prepended so this directory
# (python/websockets/) cannot shadow the installed `websockets` package.
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import stx  # noqa: E402

BOOK_TIMEOUT_SECONDS = 15


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


async def heartbeat(ws, interval=20):
    ref = 9000
    while True:
        await asyncio.sleep(interval)
        ref += 1
        await ws.send(json.dumps([None, str(ref), "phoenix", "heartbeat", {}]))


async def run(config, private_key, market, rounds):
    headers = stx.signed_headers(private_key, config["key_id"], "GET", stx.SOCKET_PATH)
    topic = f"market:{market['market_id']}"

    best_bid = market["bids"][0]["price"]
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
            await next_book_update(ws)     # settle: drain the join reply and first push

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
    parser.add_argument("--rounds", type=int, default=5, help="place/cancel pairs (default 5)")
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
