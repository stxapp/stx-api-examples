#!/usr/bin/env python3
"""Quote every combo request as a maker, over a plain Phoenix socket.

    python python/websockets/combo_maker.py
    python python/websockets/combo_maker.py --win-multiple 12 --cash-out-price 0.10
    python python/websockets/combo_maker.py --frames      # also print every frame
    python python/websockets/combo_maker.py --player <external_id>   # quote one player only

Players ask for quotes on a combo: a set of legs and a stake to buy, or a combo
they hold to cash out. Every maker joined to its `combo_quote:<user_id>` topic
receives each request as `rfq_opened`. This script quotes all of them and
prints what happens next, until Ctrl-C:

    [won]      quote_accepted, then placed: your quote was taken
    [lost]     negotiation_closed placed_elsewhere: another maker's was
    [expired]  negotiation_closed expired: your quote ran out
    [closed]   rfq_closed: the request ended, for any reason

On a buy you quote what the player wins: the stake times --win-multiple. On a
cash out you quote what you pay for everything the player sells:
--cash-out-price a contract. Each quote stands for --quote-seconds.

Combos need a read_write key: a read_only key is refused at the join.
CHANNELS.md has every frame this sends and receives.

Requires the packages in python/requirements.txt; ``./install.sh`` puts them in
python/.venv.
"""

import argparse
import asyncio
import itertools
import json
import os
import sys
import time
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

try:
    import requests
    import websockets
except ModuleNotFoundError as error:
    raise SystemExit(
        f"{error.name} is not installed. Activate the virtualenv ./install.sh made:\n"
        f"  source python/.venv/bin/activate"
    ) from None

# stx.py sits in python/. Appended so this directory cannot shadow `websockets`.
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import stx  # noqa: E402

# The socket closes after 60 s without a heartbeat, so send one every 30.
HEARTBEAT_SECONDS = 30

# Seconds to wait before reconnecting, doubling up to the last value.
BACKOFF = [1, 2, 5, 10, 30]

CENT = Decimal("0.01")


def stamp():
    return time.strftime("%H:%M:%S", time.gmtime())


def short(uuid):
    return (uuid or "-")[:8]


def quote_amount(request, win_multiple, cash_out_price):
    """The amount to quote on an rfq_opened payload, or None when it would be refused.

    The exchange derives a price from the amount, rounded to `tick_size` in the
    maker's favour, and refuses a quote whose price is not above 0 and below
    `max_price` (price_out_of_range), or, on a buy, whose stake buys no whole
    contract (below_one_contract). Money stays in Decimal, never float.
    """
    max_price = Decimal(request["max_price"])
    tick = Decimal(request["tick_size"])
    if request["side"] == "buy":
        # A buy: what the player wins on their stake.
        stake = Decimal(request["amount"])
        amount = (stake * win_multiple).quantize(CENT, rounding=ROUND_FLOOR)
        if amount <= 0:
            return None
        price = (max_price * stake / (stake + amount) / tick).to_integral_value(ROUND_CEILING) * tick
        if not 0 < price < max_price or (stake / price).to_integral_value(ROUND_FLOOR) < 1:
            return None
    else:
        # A cash out: what you pay for the whole quantity.
        quantity = Decimal(request["quantity"])
        amount = (quantity * cash_out_price).quantize(CENT, rounding=ROUND_FLOOR)
        price = (amount / quantity / tick).to_integral_value(ROUND_FLOOR) * tick
        if not 0 < price < max_price:
            return None
    return f"{amount:.2f}"


def describe(request):
    if request["side"] == "buy":
        legs = ", ".join(f"{leg.get('short_name') or short(leg['market_id'])} {leg['outcome']}"
                         for leg in request["legs"])
        return f"buy, stake {request['amount']}, {len(request['legs'])} legs: {legs}"
    return f"cash out of {request['quantity']} on combo {short(request['market_id'])}"


class Maker:
    def __init__(self, config, private_key, user_id, args):
        self.config, self.private_key, self.args = config, private_key, args
        self.topic = f"combo_quote:{user_id}"
        self.refs = itertools.count(1)
        # What each command we sent was, by its ref, so its phx_reply can be explained.
        self.pending = {}
        self.joins = 0

    async def send(self, ws, frame):
        text = json.dumps(frame, separators=(",", ":"))
        if self.args.frames:
            print(f"{stamp()}  -> {text}")
        await ws.send(text)

    async def heartbeat(self, ws):
        # On the `phoenix` topic, with a null join_ref: it belongs to no channel.
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            await self.send(ws, [None, str(next(self.refs)), "phoenix", "heartbeat", {}])

    async def session(self):
        """One connection: sign, connect, join, then quote until the socket drops."""
        # A fresh signature on every connect: the timestamp is good for 30 s.
        headers = stx.signed_headers(self.private_key, self.config["key_id"], "GET", stx.SOCKET_PATH)
        async with websockets.connect(f"{self.config['socket_url']}?vsn=2.0.0",
                                      additional_headers=headers) as ws:
            # The join's ref becomes the channel's join_ref. Every later frame to the topic
            # must carry it: the exchange silently drops a frame with an old one.
            join_ref = str(next(self.refs))
            self.pending = {join_ref: ("join", None)}
            await self.send(ws, [join_ref, join_ref, self.topic, "phx_join", {}])
            keepalive = asyncio.create_task(self.heartbeat(ws))
            try:
                async for raw in ws:
                    if self.args.frames:
                        print(f"{stamp()}  <- {raw}")
                    frame_join_ref, ref, topic, event, payload = json.loads(raw)
                    if topic == "phoenix":
                        continue  # heartbeat replies
                    if event == "phx_reply":
                        self.reply(ref, payload)
                    elif event == "rfq_opened":
                        # Every request opened after the join, from every player, your own included.
                        if self.args.player and payload.get("external_id") != self.args.player:
                            continue
                        print(f"[request]  {short(payload['rfq_id'])}  {describe(payload)}")
                        await self.quote(ws, join_ref, payload)
                    elif event == "quote_accepted":
                        # The player took your quote; the exchange is placing both orders now.
                        print(f"[accepted] {short(payload['rfq_id'])}  quote {payload['quote_id']}")
                    elif event == "placed":
                        print(f"[won]      {short(payload['rfq_id'])}  order {payload['order_id']}  "
                              f"{payload['quantity']} @ {payload['price']}  combo {payload['market_id']}")
                    elif event == "negotiation_closed":
                        # One of your quotes stopped standing, once per quote, with the reason.
                        label = {"placed_elsewhere": "[lost]   ", "expired": "[expired]"}.get(payload["reason"],
                                                                                          "[ended]  ")
                        print(f"{label}  {short(payload['rfq_id'])}  quote {payload['quote_id']}  "
                              f"{payload['reason']}")
                    elif event == "rfq_closed":
                        print(f"[closed]   {short(payload['rfq_id'])}  {payload['reason']}")
                    elif event in ("phx_error", "phx_close") and frame_join_ref == join_ref:
                        # The channel ended on a live socket: its quotes are gone. Reconnect.
                        print(f"[channel]  {event}; reconnecting")
                        return
            finally:
                keepalive.cancel()

    async def quote(self, ws, join_ref, request):
        amount = quote_amount(request, self.args.win_multiple, self.args.cash_out_price)
        if amount is None:
            print(f"[skip]     {short(request['rfq_id'])}  no amount prices inside 0 to {request['max_price']}")
            return
        ref = str(next(self.refs))
        self.pending[ref] = ("quote", request)
        await self.send(ws, [join_ref, ref, self.topic, "quote", {
            "rfq_id": request["rfq_id"],
            "amount": amount,  # a string: a JSON number is refused with malformed_money
            "expires_in_ms": int(self.args.quote_seconds * 1000),
            "client_order_id": f"maker-{short(request['rfq_id'])}",
        }])

    def reply(self, ref, payload):
        kind, request = self.pending.pop(ref, (None, None))
        status, response = payload.get("status"), payload.get("response") or {}
        if kind == "join":
            if status != "ok":
                # unmatched topic: no combos on this exchange. "This API key does not have
                # write access": a read_only key. Neither changes on a retry.
                raise SystemExit(f"Join refused: {response.get('reason')}")
            self.joins += 1
            if self.joins > 1:
                print(f"[socket]   rejoined {self.topic}, join_ref {ref}")
                return
            print(f"Joined {self.topic} on {self.config['base_url']} as a maker.")
            print(f"Buys: win {self.args.win_multiple}x the stake. Cash outs: {self.args.cash_out_price} "
                  f"a contract. Each quote stands {self.args.quote_seconds:g} s. Ctrl-C to stop.\n")
        elif kind == "quote":
            rfq = short(request["rfq_id"])
            if status != "ok":
                # e.g. cannot_quote_own_request, or not_found when the request ended first
                print(f"[refused]  {rfq}  {response.get('reason')}")
            elif request["side"] == "buy":
                print(f"[quoted]   {rfq}  win {response['amount']}  quote {response['quote_id']}")
            else:
                print(f"[quoted]   {rfq}  pay {response['amount']} for {request['quantity']}  "
                      f"quote {response['quote_id']}")

    async def run(self):
        attempt = 0
        while True:
            started = time.monotonic()
            try:
                await self.session()
            except (OSError, websockets.exceptions.WebSocketException) as error:
                # A 403 from CloudFront here, with an HTML body, means no User-Agent was sent.
                print(f"[socket]   {type(error).__name__}: {error}")
            # Quotes belong to the connection that placed them, so a drop withdrew them.
            # Reconnecting re-signs, rejoins with a fresh join_ref and starts the heartbeat again.
            attempt = 0 if time.monotonic() - started > 60 else attempt + 1
            delay = BACKOFF[min(attempt, len(BACKOFF) - 1)]
            print(f"[socket]   disconnected; quotes were withdrawn. Reconnecting in {delay} s")
            await asyncio.sleep(delay)


def main():
    parser = argparse.ArgumentParser(description="Quote every combo request as a maker.")
    parser.add_argument("--profile", help="profile in ~/.stx/credentials")
    parser.add_argument("--win-multiple", type=Decimal, default=Decimal("9"),
                        help="on a buy, quote the stake times this (default 9)")
    parser.add_argument("--cash-out-price", type=Decimal, default=Decimal("0.08"),
                        help="on a cash out, pay this a contract (default 0.08)")
    parser.add_argument("--quote-seconds", type=float, default=30, help="how long each quote stands (default 30)")
    parser.add_argument("--seconds", type=float, help="stop after this many seconds (default: run until Ctrl-C)")
    parser.add_argument("--player", help="quote only requests carrying this external_id (default: every player)")
    parser.add_argument("--frames", action="store_true", help="also print every frame sent and received")
    args = parser.parse_args()

    config = stx.load_profile(args.profile)
    private_key = stx.load_private_key(config)

    # The topic is your own user id, from GET /api/v1/me.
    headers = stx.signed_headers(private_key, config["key_id"], "GET", "/api/v1/me")
    try:
        me = requests.get(config["base_url"] + "/api/v1/me", headers=headers, timeout=15)
    except requests.exceptions.ConnectionError as error:
        sys.exit(stx.unreachable(config["base_url"], error))
    me.raise_for_status()

    maker = Maker(config, private_key, me.json()["me"]["user_id"], args)
    try:
        asyncio.run(asyncio.wait_for(maker.run(), args.seconds) if args.seconds else maker.run())
    except (KeyboardInterrupt, asyncio.TimeoutError):
        print("\nStopped. Quotes still standing were withdrawn with the connection.")


if __name__ == "__main__":
    main()
