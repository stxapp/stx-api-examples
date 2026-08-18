#!/usr/bin/env python3
"""
STX socket watcher - run this in one console and trade in another.

Streams, on a single authenticated connection:

  * the aggregated order book for one market   (public)
  * your order state changes                   (active_orders)
  * your fills                                 (active_trades)
  * your positions and available balance       (active_positions, portfolio)
  * your settlements as they realise            (active_settlements)

    python stx_watch.py --profile ontario-staging
    python stx_watch.py --profile ontario-staging --market <market_id>
    python stx_watch.py --profile ontario-staging --cancel-on-disconnect

Then, in a second console:

    python stx_quickstart.py --profile ontario-staging roundtrip

and watch the order appear and disappear here.

Configuration comes from the same ~/.stx/credentials profile stx_quickstart.py uses.
Private channels additionally need `user_id` in the profile, because channel topics
are scoped by user id.

Questions: https://discord.gg/yF9eVzPzNZ
"""

import argparse
import asyncio
import base64
import configparser
import json
import os
import sys
import time
from datetime import datetime

import requests
import websockets
from cryptography.hazmat.primitives import serialization

GRAPHQL_PATH = "/api/graphql"
SOCKET_PATH = "/socket/websocket"
HOSTS = {
    ("ontario", "production"): "api.on.stxapp.ca",
    ("ontario", "staging"): "staging.on.sportsxapp.com",
}
PROFILE_FILE = os.path.expanduser("~/.stx/credentials")


def load_profile(name=None):
    name = name or os.environ.get("STX_PROFILE", "default")
    values = {}
    if os.path.exists(PROFILE_FILE):
        parser = configparser.ConfigParser()
        parser.read(PROFILE_FILE)
        if parser.has_section(name):
            values = dict(parser.items(name))
        elif name != "default":
            sys.exit(f"Profile {name!r} not found in {PROFILE_FILE}. Available: {parser.sections()}")

    def pick(env_var, key, default=None):
        return os.environ.get(env_var) or values.get(key) or default

    region, env = pick("STX_REGION", "region", "ontario"), pick("STX_ENV", "env", "staging")
    host = pick("STX_HOST", "host") or HOSTS.get((region, env))
    if not host:
        extra = (" US endpoints are not published yet - set `host` explicitly."
                 if region == "us" else "")
        sys.exit(f"No host known for region={region!r} env={env!r}.{extra} "
                 f"Set STX_HOST or `host` in the profile.")
    return {
        "profile": name,
        "host": host,
        "key_id": pick("STX_KEY_ID", "key_id"),
        "key_path": os.path.expanduser(pick("STX_PRIVATE_KEY", "private_key", "~/.stx/my_key.pem")),
        "user_id": pick("STX_USER_ID", "user_id"),
    }


def signed_headers(cfg, method, path, prefix=""):
    """See stx_quickstart.py for the full explanation of the signing scheme."""
    with open(cfg["key_path"], "rb") as fh:
        private_key = serialization.load_pem_private_key(fh.read(), password=None)
    timestamp = str(int(time.time() * 1000))
    signature = base64.b64encode(
        private_key.sign(f"{timestamp}{method.upper()}{path}".encode("utf-8"))
    ).decode()
    return {
        f"{prefix}STX-ACCESS-KEY": cfg["key_id"],
        f"{prefix}STX-ACCESS-TIMESTAMP": timestamp,
        f"{prefix}STX-ACCESS-SIGNATURE": signature,
    }


# Same selection order as stx_quickstart.py, so running the two side by side
# lands on the same market and you see your own orders hit the book you are watching.
SPORTS = ["Baseball", "Basketball", "Boxing", "Cricket", "Football", "Golf",
          "Hockey", "MMA", "Racing", "Soccer", "Tennis"]

QUERY = """query M($input: MarketInfosInput) {
  marketInfos(input: $input) { marketId symbol title status bids { price } offers { price } }
}"""


def pick_market(cfg):
    """First market with a resting book: sports if any are trading, else anything."""
    def fetch(sports_only):
        query_input = {"limit": 200, "status": ["OPEN"]}
        if sports_only:
            query_input["sports"] = SPORTS
        r = requests.post(f"https://{cfg['host']}{GRAPHQL_PATH}",
                          json={"query": QUERY, "variables": {"input": query_input}}, timeout=20)
        r.raise_for_status()
        return r.json()["data"]["marketInfos"]

    for sports_only in (True, False):
        for market in fetch(sports_only):
            if market["bids"] or market["offers"]:
                return market
    sys.exit("No open market with a book to watch.")


def stamp():
    return datetime.now().strftime("%H:%M:%S")


# --------------------------------------------------------------------------
# Rendering. Book prices arrive in currency units; order and trade prices over
# GraphQL and these channels are integer cents. Keep the two straight.
# --------------------------------------------------------------------------

def render_book(payload):
    book = payload["ob"]
    bid = book["b"][0] if book["b"] else None
    offer = book["o"][0] if book["o"] else None
    top = (f"{bid['q']:>6.0f} @ {bid['p']:>7.2f}" if bid else " " * 16) + "   |   " + \
          (f"{offer['p']:<7.2f} @ {offer['q']:<6.0f}" if offer else "")
    print(f"{stamp()}  BOOK   {top}   ({len(book['b'])}x{len(book['o'])} levels)")


# Bulk snapshot events arrive once on join; summarise rather than dump.
_SNAPSHOTS = {"all_orders": "orders", "all_trades": "trades",
              "all_positions": "positions", "updated_positions": "positions",
              "all_settlements": "settlements", "new_settlements": "settlements"}


def render_generic(label, payload):
    """Channel payloads vary; print the interesting keys, fall back to compact JSON."""
    if isinstance(payload, dict):
        event = payload.get("event")
        if event in _SNAPSHOTS:
            rows = payload.get(_SNAPSHOTS[event]) or []
            print(f"{stamp()}  {label:<7}{event}: {len(rows)} row(s)")
            return
        interesting = {k: v for k, v in payload.items()
                       if k in ("id", "status", "price", "quantity", "filled", "action",
                                "market_id", "client_order_id", "available_balance",
                                "total_liability", "contracts", "reason")}
        if interesting:
            body = "  ".join(f"{k}={v}" for k, v in interesting.items())
            print(f"{stamp()}  {label:<7}{body}")
            return
    text = json.dumps(payload, separators=(",", ":"))
    print(f"{stamp()}  {label:<7}{text[:220]}")


async def heartbeat(ws, interval=15):
    """Sockets close after ~20s of silence. Keep sending these."""
    ref = 9000
    while True:
        await asyncio.sleep(interval)
        ref += 1
        await ws.send(json.dumps([None, str(ref), "phoenix", "heartbeat", {}]))


async def order_pings(ws, topic, join_ref, interval):
    """Cancel-on-disconnect requires periodic pings on the active_orders channel.

    If the server stops seeing them for longer than the negotiated ping_timeout it
    treats the channel as dead and cancels every order flagged cancelOnDisconnect.
    """
    ref = 7000
    while True:
        await asyncio.sleep(interval)
        ref += 1
        await ws.send(json.dumps([join_ref, str(ref), topic, "ping", {}]))


async def watch(cfg, market, cancel_on_disconnect, ping_timeout_ms):
    private = bool(cfg["user_id"])
    if not private:
        print("! no user_id in profile - private channels skipped, book only\n")

    url = f"wss://{cfg['host']}{SOCKET_PATH}?vsn=2.0.0"
    headers = signed_headers(cfg, "GET", SOCKET_PATH, prefix="X-") if cfg["key_id"] else None

    async with websockets.connect(url, additional_headers=headers) as ws:
        tasks = [asyncio.create_task(heartbeat(ws))]

        topics = {f"market:{market['marketId']}": "BOOK"}
        if private:
            uid = cfg["user_id"]
            topics.update({
                f"active_orders:{uid}": "ORDER",
                f"active_trades:{uid}": "TRADE",
                f"active_positions:{uid}": "POS",
                f"active_settlements:{uid}": "SETTLE",
                f"portfolio:{uid}": "WALLET",
            })

        orders_topic = f"active_orders:{cfg['user_id']}" if private else None
        for i, topic in enumerate(topics):
            payload = {}
            if topic == orders_topic and cancel_on_disconnect:
                payload = {"cancel_on_disconnect": True, "ping_timeout": ping_timeout_ms}
            await ws.send(json.dumps([str(i), str(i), topic, "phx_join", payload]))
            if topic == orders_topic:
                tasks.append(asyncio.create_task(
                    order_pings(ws, topic, str(i), max(1, ping_timeout_ms // 1000 // 2))))

        # Ask for an immediate book snapshot rather than waiting for the next tick.
        await asyncio.sleep(0.6)
        await ws.send(json.dumps(["0", "snap", f"market:{market['marketId']}",
                                  "request_snapshot", {}]))

        print(f"watching {market['symbol']}  ({len(topics)} channels)   ctrl-c to stop\n")
        try:
            while True:
                frame = json.loads(await ws.recv())
                _join_ref, _ref, topic, event, payload = frame
                label = topics.get(topic, topic.split(":")[0].upper())

                if event == "phx_reply":
                    status = payload.get("status")
                    response = payload.get("response") or {}
                    if status != "ok":
                        print(f"{stamp()}  JOIN    {label} FAILED: {response}")
                    elif response.get("ping") == "pong":
                        pass          # keepalive ack for cancel-on-disconnect
                    elif response:
                        print(f"{stamp()}  JOIN    {label} ok  {json.dumps(response)[:120]}")
                    continue
                if event in ("phx_close", "phx_error"):
                    print(f"{stamp()}  {event.upper()} on {topic}")
                    continue
                if event == "order_book_update":
                    render_book(payload)
                    continue
                if event in ("presence_state", "presence_diff"):
                    continue
                render_generic(f"{label}", {"event": event, **(payload if isinstance(payload, dict) else {})}
                               if not isinstance(payload, dict) or "event" not in payload else payload)
        finally:
            for task in tasks:
                task.cancel()


def main():
    parser = argparse.ArgumentParser(description="Stream STX book and account events.")
    parser.add_argument("--profile", help="profile in ~/.stx/credentials")
    parser.add_argument("--market", help="market id to watch (default: first with a book)")
    parser.add_argument("--cancel-on-disconnect", action="store_true",
                        help="ask the exchange to cancel flagged orders if this process dies")
    parser.add_argument("--ping-timeout", type=int, default=10000,
                        help="cancel-on-disconnect timeout in ms (default 10000)")
    args = parser.parse_args()

    cfg = load_profile(args.profile)
    print(f"[profile {cfg['profile']} -> {cfg['host']}]")

    if args.market:
        market = {"marketId": args.market, "symbol": args.market[:8]}
    else:
        market = pick_market(cfg)

    try:
        asyncio.run(watch(cfg, market, args.cancel_on_disconnect, args.ping_timeout))
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
