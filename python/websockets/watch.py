#!/usr/bin/env python3
"""STX socket watcher - one connection, the book plus all of your own activity.

Run this in one console and trade in another:

    python python/websockets/watch.py
    python python/websockets/watch.py --market <market_id_or_symbol>
    python python/websockets/watch.py --cancel-on-disconnect

    # in a second console
    python python/rest/quickstart.py roundtrip

Streams, on a single authenticated socket:

  * the aggregated order book for one market   orderbook
  * that market's price summary                ticker
  * your order state changes                   orders:<user_id>
  * your own executions                        fills:<user_id>
  * your positions                             positions:<user_id>
  * your settlements as they realise           settlements:<user_id>
  * your balances                              balances:<user_id>

These are the dollar-format topics: money arrives as a decimal string in
dollars, matching /api/v1. The older cents topics (active_orders, active_trades,
active_positions, active_settlements, portfolio and market:<market_id>) still
exist but are not used here - see CHANNELS.md.

`orderbook` and `ticker` are single public topics covering every market,
narrowed by the join payload, so watching ten markets is one join rather than
ten. They narrow differently, and the difference is worth seeing: `orderbook`
takes `market_ids` and filters server-side, while `ticker` takes only `sports`
and `competitions`, so pinning it to one market means filtering client-side as
well. Both halves are done below.

The user id in the private topics comes from GET /api/v1/me, which this fetches
at startup - there is no other way to read it.

Requires the packages in python/requirements.txt; ``./install.sh`` puts them in
python/.venv.
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime

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

# Two independent timers, and the socket heartbeat only resets one of them.
#
#   socket keep-alive     `heartbeat` on the `phoenix` topic     60s
#   cancel_on_disconnect  `ping` on the orders: topic            whatever you
#                                                                asked for
#
# Miss the first and the connection closes, which is at least obvious. Miss the
# second and your flagged orders are cancelled on a connection that is still
# up, which is not. Both run on their own timers below.
HEARTBEAT_SECONDS = 20

# `unmatched topic` is the server saying it has never heard of the topic, which
# on a correct client means the host predates it. Worth naming, because it looks
# identical to a typo and is the single most likely failure while the
# dollar-format topics are still rolling out.
UNMATCHED_TOPIC_HINT = (
    "\n  'unmatched topic' means the server does not know that topic.\n"
    "  The dollar-format topics this watcher joins need a host running them;\n"
    "  older deployments carry only the legacy cents topics. See CHANNELS.md.\n"
)

# ping_timeout is clamped server-side to 5000-20000 ms. Values outside that are
# silently pulled to the nearest bound, and a non-integer fails the join with
# {"ping_timeout": "Must be an integer"}.
DEFAULT_PING_TIMEOUT_MS = 10000


# Every stamped line goes through `line()` so BOOK, ORDER and JOIN share one
# column. The separator is a space of its own rather than padding, because a
# label exactly LABEL_WIDTH wide would otherwise run into the text: USER_INFO,
# reachable via --topic, is 9 characters.
LABEL_WIDTH = 9


def line(label, rest):
    """One output row: time, label column, then whatever the caller has."""
    return f"{stamp()}  {label:<{LABEL_WIDTH}} {rest}"


def stamp():
    return datetime.now().strftime("%H:%M:%S")


# ---------------------------------------------------------------------------
# REST calls made once at startup: who we are, and what to watch.
# ---------------------------------------------------------------------------


def rest_get(config, private_key, path):
    headers = stx.signed_headers(private_key, config["key_id"], "GET", path)
    try:
        response = requests.get(config["base_url"] + path, headers=headers, timeout=15)
    except requests.exceptions.ConnectionError as error:
        sys.exit(stx.unreachable(config["base_url"], error))
    if not response.ok:
        sys.exit(f"GET {path} -> HTTP {response.status_code}: {response.text[:300]}")
    return response.json()


def pick_market(config, private_key, wanted=None):
    """One market to watch, by id or symbol, or the deepest book if not named.

    Always returns the market as the API describes it, never a stub built from
    the argument. `orderbook` takes UUIDs only - it drops anything that is not
    one and then rejects the join with `market_ids_required` - and `ticker`
    needs the market's `sport` and `competition` to narrow on. Neither can be
    had from a symbol without asking.
    """
    markets = rest_get(config, private_key, "/api/v1/markets?status=open&limit=200")["markets"]
    live = [m for m in markets if m.get("trading") and m.get("status") == "open"]
    if not live:
        sys.exit("No tradeable market to watch.")

    if wanted:
        for market in live:
            if wanted in (market["market_id"], market["symbol"]):
                return market
        sys.exit(f"Market {wanted} is not open and tradeable right now.")

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
# Rendering. Every price here is a dollar string, as on /api/v1.
# ---------------------------------------------------------------------------


def render_book(payload):
    """One `book` push from the `orderbook` topic.

    Levels are flat: `bids` and `offers` hold `price`, `quantity`, `liquidity`,
    `total_quantity` and `total_liquidity`, all dollar or quantity strings. The
    legacy `market:` topic nested them under `ob.b`/`ob.o` with `p`/`q` keys.

    Every push is a COMPLETE snapshot of that market's book, not a delta.
    Replace whatever you hold for this market_id rather than merging into it.
    """
    bids, offers = payload.get("bids") or [], payload.get("offers") or []
    bid = f"{bids[0]['quantity']:>7} @ {stx.fmt_money(bids[0]['price']):>6}" if bids else " " * 16
    offer = f"{stx.fmt_money(offers[0]['price']):<6} @ {offers[0]['quantity']:<7}" if offers else ""
    print(line("BOOK", f"{bid}   |   {offer}   ({len(bids)}x{len(offers)} levels)"))


def render_ticker(payload):
    """One `ticker` push: a whole-market price summary, not a book.

    Complete every time rather than a diff, so there is nothing to merge. It
    fires when price, the top of book, volume or open interest moves - which
    includes a new resting level, so an order placed under the touch shows up
    here as a depth change even though it did not move the price.

    Any field can be null on a market that has not traded or has an empty side.
    """
    def shown(key):
        value = payload.get(key)
        return "-" if value is None else value

    print(line("MARKET", f"last={stx.fmt_money(payload.get('last_traded_price'))}  "
                         f"vol={shown('total_volume')}  "
                         f"oi={shown('open_interest')}  "
                         f"{shown('bid_depth')}x{shown('offer_depth')}"))


# Snapshot events arrive once on join and can carry hundreds of rows. Summarise.
# The dollar topics keep the legacy event names, so a client that already
# handles active_orders needs no re-tagging when it moves to orders:.
# settlements: has no join snapshot; new_settlements arrives as they realise.
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
        print(line(label, f"{event}: {len(rows)} row(s)"))
        return

    if isinstance(payload, dict):
        fields = {k: v for k, v in payload.items() if k in INTERESTING}
        if fields:
            body = "  ".join(f"{k}={v}" for k, v in fields.items())
            print(line(label, f"{event}  {body}"))
            return

    print(line(label, f"{event}  {json.dumps(payload, separators=(',', ':'))[:200]}"))


# ---------------------------------------------------------------------------
# The socket itself
# ---------------------------------------------------------------------------


async def watch(config, private_key, user_id, market, cancel_on_disconnect, ping_timeout_ms,
                extra_topics=()):
    # The handshake signs GET against /socket/websocket with the query string
    # DROPPED - `?vsn=2.0.0` is on the URL but not in the signed message.
    # Phoenix's transport only surfaces x-* headers, so the names must be exact
    # or the socket connects without credentials and then fails on the first
    # private channel with "unauthorized".
    headers = stx.signed_headers(private_key, config["key_id"], "GET", stx.SOCKET_PATH)
    url = f"{config['socket_url']}?vsn=2.0.0"

    orders_topic = f"orders:{user_id}"

    # topic -> (label, join payload). `orderbook` is the one topic that REQUIRES
    # a payload: it is public and covers every market, so a join naming no
    # usable market_id is rejected with {"reason": "market_ids_required"}
    # rather than quietly subscribing you to the firehose.
    # `ticker` has no market_ids filter, so narrow it as far as the server
    # allows and drop the rest below. These values are passed through from the
    # REST market verbatim: the ticker payload's `sport` and `competition` come
    # from the same server-side field, so they match without any normalising.
    ticker_filter = {}
    if market.get("sport"):
        ticker_filter["sports"] = [market["sport"]]
    if market.get("competition"):
        ticker_filter["competitions"] = [market["competition"]]

    topics = {
        "orderbook": ("BOOK", {"market_ids": [market["market_id"]]}),
        "ticker": ("MARKET", ticker_filter),
        orders_topic: ("ORDER", {}),
        f"fills:{user_id}": ("FILL", {}),
        f"positions:{user_id}": ("POS", {}),
        f"settlements:{user_id}": ("SETTLE", {}),
        f"balances:{user_id}": ("WALLET", {}),
    }

    if cancel_on_disconnect:
        topics[orders_topic] = ("ORDER", {
            "cancel_on_disconnect": True,
            "ping_timeout": ping_timeout_ms,
        })

    # Anything passed with --topic joins on the same socket. The join loop, the
    # label lookup and render_event are all topic-agnostic already, so a topic
    # this file has never heard of prints its frames like any other.
    for topic in extra_topics:
        topics.setdefault(topic.replace("<user_id>", user_id),
                          (topic.split(":")[0].upper(), {}))

    warned = False

    async with websockets.connect(url, additional_headers=headers) as ws:
        tasks = [asyncio.create_task(socket_heartbeat(ws))]

        for index, (topic, (_label, payload)) in enumerate(topics.items()):
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
                label = topics.get(topic, (topic.split(":")[0].upper(),))[0]

                if event == "phx_reply":
                    response = payload.get("response") or {}
                    if payload.get("status") != "ok":
                        print(line("JOIN", f"{label} FAILED on {topic}: {response}"))
                        # Printed once, however many topics are missing: on an
                        # older host every dollar topic fails the same way and
                        # the reason is the same for all of them.
                        if response.get("reason") == "unmatched topic" and not warned:
                            print(UNMATCHED_TOPIC_HINT, file=sys.stderr)
                            warned = True
                    elif response.get("ping") == "pong":
                        pass                     # cancel_on_disconnect keepalive ack
                    elif response.get("cancel_on_disconnect"):
                        print(line("JOIN", f"{label} ok, cancel_on_disconnect "
                                           f"ping_timeout={response.get('ping_timeout')}ms"))
                    elif "selected_sports" in response:
                        # The echo is the only signal that a filter applied. A
                        # value the server did not recognise is dropped in
                        # silence and comes back as null, meaning no filter.
                        print(line("JOIN", f"{label} ok, sports="
                                           f"{response['selected_sports']} "
                                           f"competitions="
                                           f"{response['selected_competitions']}"))
                    elif "selected_market_ids" in response:
                        # Echoed by `orderbook` and by the private topics, which
                        # take the same filter. A mistyped id shows up here
                        # rather than as silence.
                        #
                        # null means NO filter, not an empty one - the private
                        # topics are joined without market_ids above, so that is
                        # their normal reply and everything on the account
                        # arrives. Only `orderbook` requires a non-empty list.
                        applied = response["selected_market_ids"]
                        scope = f"markets={len(applied)}" if applied else "no market filter"
                        print(line("JOIN", f"{label} ok, {scope}"))
                    continue

                if event in ("phx_close", "phx_error"):
                    print(line(event.upper(), f"on {topic}"))
                    continue

                if event == "book":
                    render_book(payload)
                    continue

                if event == "ticker":
                    # One global topic: every market in the sport arrives here,
                    # so the market filter that `orderbook` did server-side has
                    # to be done by hand.
                    if payload.get("market_id") == market["market_id"]:
                        render_ticker(payload)
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
    parser.add_argument("--topic", action="append", default=[], metavar="TOPIC",
                        help="extra topic to join, repeatable. <user_id> is "
                             "substituted, e.g. --topic 'user_info:<user_id>'")
    args = parser.parse_args()

    config = stx.load_profile(args.profile)
    private_key = stx.load_private_key(config)
    print(f"[{config['profile']} -> {config['base_url']}]", file=sys.stderr)

    user_id = rest_get(config, private_key, "/api/v1/me")["me"]["user_id"]

    market = pick_market(config, private_key, args.market)

    try:
        asyncio.run(watch(config, private_key, user_id, market,
                          args.cancel_on_disconnect, args.ping_timeout, args.topic))
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
