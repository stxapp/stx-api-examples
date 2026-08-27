#!/usr/bin/env python3
"""STX socket watcher - one connection, the book plus all of your own activity.

Run this in one console and trade in another:

    python python/websockets/watch.py
    python python/websockets/watch.py --market <market_id_or_symbol>
    python python/websockets/watch.py --cancel-on-disconnect

    # in a second console
    python python/rest/quickstart.py roundtrip

Streams, on a single authenticated socket:

  * the aggregated order book for one market   market:<market_id>
  * your order state changes                   active_orders:<user_id>
  * your fills                                 active_trades:<user_id>
  * your positions and balance                 active_positions, portfolio
  * your settlements as they realise           active_settlements:<user_id>

The user id in those topics comes from GET /api/v1/me, which this fetches at
startup - there is no other way to read it.

Requires the packages in python/requirements.txt; ``./install.sh`` puts them in
python/.venv.
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime

import requests
import websockets

# stx.py sits in python/. Appended rather than prepended so this directory
# (python/websockets/) cannot shadow the installed `websockets` package.
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import stx  # noqa: E402

# Two independent timers, and the socket heartbeat only resets one of them.
#
#   socket keep-alive     `heartbeat` on the `phoenix` topic     60s
#   cancel_on_disconnect  `ping` on the active_orders topic      whatever you
#                                                                asked for
#
# Miss the first and the connection closes, which is at least obvious. Miss the
# second and your flagged orders are cancelled on a connection that is still
# up, which is not. Both run on their own timers below.
HEARTBEAT_SECONDS = 20

# ping_timeout is clamped server-side to 5000-20000 ms. Values outside that are
# silently pulled to the nearest bound, and a non-integer fails the join with
# {"ping_timeout": "Must be an integer"}.
DEFAULT_PING_TIMEOUT_MS = 10000


def stamp():
    return datetime.now().strftime("%H:%M:%S")


# ---------------------------------------------------------------------------
# REST calls made once at startup: who we are, and what to watch.
# ---------------------------------------------------------------------------


def rest_get(config, private_key, path):
    headers = stx.signed_headers(private_key, config["key_id"], "GET", path)
    response = requests.get(config["base_url"] + path, headers=headers, timeout=15)
    if not response.ok:
        sys.exit(f"GET {path} -> HTTP {response.status_code}: {response.text[:300]}")
    return response.json()


def pick_market(config, private_key):
    """The tradeable market with the deepest book, so there is something to see."""
    markets = rest_get(config, private_key, "/api/v1/markets?status=open&limit=200")["markets"]
    live = [m for m in markets if m.get("trading") and m.get("status") == "open"]
    if not live:
        sys.exit("No tradeable market to watch.")
    return max(live, key=lambda m: len(m.get("bids") or []) + len(m.get("offers") or []))


# ---------------------------------------------------------------------------
# Phoenix channel protocol, version 2.
#
# Every frame on the wire is a five-element JSON array:
#
#     [join_ref, ref, topic, event, payload]
#
# `join_ref` is the ref used for that topic's phx_join, and every later message
# to that topic must repeat it. A frame with the wrong join_ref is dropped with
# no reply, which looks exactly like the server ignoring you.
# ---------------------------------------------------------------------------


async def send(ws, join_ref, ref, topic, event, payload):
    await ws.send(json.dumps([join_ref, str(ref), topic, event, payload]))


async def socket_heartbeat(ws, interval=HEARTBEAT_SECONDS):
    """Keeps the connection open. One per socket, not one per channel."""
    ref = 9000
    while True:
        await asyncio.sleep(interval)
        ref += 1
        await send(ws, None, ref, "phoenix", "heartbeat", {})


async def order_pings(ws, topic, join_ref, ping_timeout_ms):
    """Keeps cancel_on_disconnect orders alive.

    Sent on a timer at half the negotiated timeout, not in response to traffic:
    a quiet market produces no traffic and the deadline does not care. The
    server replies {"ping": "pong", "ttl": <ping_timeout>} and resets its clock.
    """
    interval = max(1.0, ping_timeout_ms / 1000.0 / 2)
    ref = 7000
    while True:
        await asyncio.sleep(interval)
        ref += 1
        await send(ws, join_ref, ref, topic, "ping", {})


# ---------------------------------------------------------------------------
# Rendering. Every price here is integer cents.
# ---------------------------------------------------------------------------


def render_book(payload):
    book = payload["ob"]
    bids, offers = book.get("b") or [], book.get("o") or []
    bid = f"{bids[0]['q']:>6} @ {bids[0]['p']:>4}c" if bids else " " * 14
    offer = f"{offers[0]['p']:<4}c @ {offers[0]['q']:<6}" if offers else ""
    print(f"{stamp()}  BOOK   {bid}   |   {offer}   ({len(bids)}x{len(offers)} levels)")


# Snapshot events arrive once on join and can carry hundreds of rows. Summarise.
SNAPSHOTS = {
    "all_orders": "orders",
    "all_trades": "trades",
    "all_positions": "positions",
    "new_positions": "positions",
    "all_settlements": "settlements",
    "new_settlements": "settlements",
}

INTERESTING = (
    "id", "status", "price", "quantity", "filled", "action", "market_id",
    "client_order_id", "available_balance", "total_liability", "contracts",
    "cancellation_reason", "rejection_reason",
)


def render_event(label, event, payload):
    if event in SNAPSHOTS and isinstance(payload, dict):
        rows = payload.get(SNAPSHOTS[event]) or []
        print(f"{stamp()}  {label:<8}{event}: {len(rows)} row(s)")
        return

    if isinstance(payload, dict):
        fields = {k: v for k, v in payload.items() if k in INTERESTING}
        if fields:
            body = "  ".join(f"{k}={v}" for k, v in fields.items())
            print(f"{stamp()}  {label:<8}{event}  {body}")
            return

    print(f"{stamp()}  {label:<8}{event}  {json.dumps(payload, separators=(',', ':'))[:200]}")


# ---------------------------------------------------------------------------
# The socket itself
# ---------------------------------------------------------------------------


async def watch(config, private_key, user_id, market, cancel_on_disconnect, ping_timeout_ms):
    # The handshake signs GET against /socket/websocket with the query string
    # DROPPED - `?vsn=2.0.0` is on the URL but not in the signed message.
    # Phoenix's transport only surfaces x-* headers, so the names must be exact
    # or the socket connects without credentials and then fails on the first
    # private channel with "unauthorized".
    headers = stx.signed_headers(private_key, config["key_id"], "GET", stx.SOCKET_PATH)
    url = f"{config['socket_url']}?vsn=2.0.0"

    market_topic = f"market:{market['market_id']}"
    orders_topic = f"active_orders:{user_id}"
    topics = {
        market_topic: "BOOK",
        orders_topic: "ORDER",
        f"active_trades:{user_id}": "TRADE",
        f"active_positions:{user_id}": "POS",
        f"active_settlements:{user_id}": "SETTLE",
        f"portfolio:{user_id}": "WALLET",
    }

    async with websockets.connect(url, additional_headers=headers) as ws:
        tasks = [asyncio.create_task(socket_heartbeat(ws))]

        for index, topic in enumerate(topics):
            payload = {}
            if topic == orders_topic and cancel_on_disconnect:
                payload = {
                    "cancel_on_disconnect": True,
                    "ping_timeout": ping_timeout_ms,
                }
            await send(ws, str(index), index, topic, "phx_join", payload)
            if topic == orders_topic and cancel_on_disconnect:
                tasks.append(asyncio.create_task(
                    order_pings(ws, topic, str(index), ping_timeout_ms)
                ))

        print(f"watching {market['symbol']}  ({len(topics)} channels)   ctrl-c to stop")
        if cancel_on_disconnect:
            print(f"cancel_on_disconnect on, pinging inside {ping_timeout_ms}ms")
        print()

        try:
            while True:
                join_ref, ref, topic, event, payload = json.loads(await ws.recv())
                label = topics.get(topic, topic.split(":")[0].upper())

                if event == "phx_reply":
                    response = payload.get("response") or {}
                    if payload.get("status") != "ok":
                        print(f"{stamp()}  JOIN    {label} FAILED: {response}")
                    elif response.get("ping") == "pong":
                        pass                     # cancel_on_disconnect keepalive ack
                    elif response.get("cancel_on_disconnect"):
                        print(f"{stamp()}  JOIN    {label} ok, cancel_on_disconnect "
                              f"ping_timeout={response.get('ping_timeout')}ms")
                    elif "ob" in response:
                        # A market: join replies with the current book, so there
                        # is no need to ask for a snapshot first.
                        render_book(response)
                    continue

                if event in ("phx_close", "phx_error"):
                    # On market:* this also means the market reached a terminal
                    # status and the server dropped us - not a network fault.
                    print(f"{stamp()}  {event.upper()} on {topic}")
                    continue

                if event == "order_book_update":
                    render_book(payload)
                    continue

                if event in ("presence_state", "presence_diff"):
                    continue

                render_event(label, event, payload)
        finally:
            for task in tasks:
                task.cancel()


def main():
    parser = argparse.ArgumentParser(description="Stream the STX book and your own activity.")
    parser.add_argument("--profile", help="profile in ~/.stx/credentials")
    parser.add_argument("--market", help="market id or symbol (default: deepest book)")
    parser.add_argument("--cancel-on-disconnect", action="store_true",
                        help="ask the exchange to cancel flagged orders if this process dies")
    parser.add_argument("--ping-timeout", type=int, default=DEFAULT_PING_TIMEOUT_MS,
                        help=f"cancel_on_disconnect timeout in ms, 5000-20000 "
                             f"(default {DEFAULT_PING_TIMEOUT_MS})")
    args = parser.parse_args()

    config = stx.load_profile(args.profile)
    private_key = stx.load_private_key(config)
    print(f"[{config['profile']} -> {config['base_url']}]", file=sys.stderr)

    user_id = rest_get(config, private_key, "/api/v1/me")["me"]["user_id"]

    if args.market:
        market = {"market_id": args.market, "symbol": args.market}
    else:
        market = pick_market(config, private_key)

    try:
        asyncio.run(watch(config, private_key, user_id, market,
                          args.cancel_on_disconnect, args.ping_timeout))
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
