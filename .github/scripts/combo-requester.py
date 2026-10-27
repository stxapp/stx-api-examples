#!/usr/bin/env python3
"""The player side of a combo, for the Live workflow: buy one small combo from
the best maker quote, then cash it out, so the run ends holding nothing.

    python .github/scripts/combo-requester.py [--stake 1.00] [--wait 30] [--gather 3]
    python .github/scripts/combo-requester.py --player-id   # print "Player: <external_id>" and exit

Runs beside python/websockets/combo_maker.py or javascript/websockets/combo_maker.mjs,
started as a different account, so the maker under test can quote these requests.
Once the first quote is live it keeps reading for --gather seconds, so a quote
that lands a moment after another maker's is still in the running, then accepts
the best one at its exact amount.

Prints, for the workflow to check:
    Request: <rfq_id>  stake <stake>
    Bought:  <quantity> @ <price>  quote <quote_id>  combo <market_id>
    Cash out: <rfq_id>  selling <quantity>
    Sold:    <quantity> @ <price>  quote <quote_id>
    Final:   position <n>  open orders <n>
Exits 1 if no quote was taken on either side or a position is left.
"""

import argparse
import asyncio
import itertools
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import requests
import websockets

sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "python"))
import stx  # noqa: E402


class Rest:
    def __init__(self, config, key):
        self.config, self.key = config, key

    def call(self, method, path):
        headers = stx.signed_headers(self.key, self.config["key_id"], method, path)
        response = requests.request(method, self.config["base_url"] + path, headers=headers, timeout=15)
        response.raise_for_status()
        return response.json()


class Player:
    """The rfq:<user_id> channel: commands answered by ref, pushes queued."""

    def __init__(self, ws, topic):
        self.ws, self.topic = ws, topic
        self.refs = itertools.count(1)
        self.join_ref = None
        self.replies = {}
        self.pushes = asyncio.Queue()

    async def reader(self):
        async for raw in self.ws:
            _join_ref, ref, topic, event, payload = json.loads(raw)
            if topic != self.topic:
                continue
            if event == "phx_reply" and ref in self.replies:
                self.replies.pop(ref).set_result(payload)
            elif event != "phx_reply":
                await self.pushes.put((event, payload))

    async def heartbeat(self):
        while True:
            await asyncio.sleep(30)
            await self.ws.send(json.dumps([None, str(next(self.refs)), "phoenix", "heartbeat", {}]))

    async def send(self, event, payload):
        ref = str(next(self.refs))
        if event == "phx_join":
            self.join_ref = ref
        future = asyncio.get_running_loop().create_future()
        self.replies[ref] = future
        await self.ws.send(json.dumps([self.join_ref, ref, self.topic, event, payload]))
        reply = await asyncio.wait_for(future, 15)
        return reply.get("status"), reply.get("response") or {}

    async def take_best(self, rfq_id, wait, gather):
        """Accept the best quote on rfq_id: wait up to `wait` s for one, then `gather` s more."""
        best, deadline = None, time.monotonic() + wait
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            try:
                event, data = await asyncio.wait_for(self.pushes.get(), left)
            except asyncio.TimeoutError:
                break
            if data.get("rfq_id") != rfq_id:
                continue
            if event == "negotiation_closed":
                print(f"Ended:   {data.get('reason')}")
                return None
            if event == "best_quote":
                best = data if data.get("quote_id") else None
                print(f"[quote]  {data.get('amount') or 'none live'}  quote {data.get('quote_id')}")
                if best and deadline - time.monotonic() > gather:
                    deadline = time.monotonic() + gather
        for _ in range(5):
            if not best:
                return None
            status, response = await self.send("accept", {
                "rfq_id": rfq_id, "quote_id": best["quote_id"], "amount": best["amount"]})
            if status == "ok":
                return {**response, "quote_id": best["quote_id"]}
            if response.get("reason") != "price_moved":
                print(f"Refused: {response.get('reason')}")
                return None
            # The best moved under the accept: the reply carries the live best.
            best = response if response.get("quote_id") else None
        return None


def pick_legs(rest, count=2):
    """Open markets from different events starting at least 2 hours out, never a combo."""
    soon = datetime.now(timezone.utc) + timedelta(hours=2)
    legs, events, path = [], set(), "/api/v1/markets?status=open&trading=true&limit=200"
    for _ in range(5):
        page = rest.call("GET", path)
        for m in page.get("markets", []):
            start = m.get("event_start")
            if m.get("event_type") == "combo" or m.get("archived") or m.get("event_status") not in (None, "scheduled"):
                continue
            if not start or datetime.fromisoformat(start.replace("Z", "+00:00")) <= soon:
                continue
            event = m.get("event_id") or m["market_id"]
            if event in events:
                continue
            events.add(event)
            legs.append({"market_id": m["market_id"], "outcome": "won"})
            if len(legs) == count:
                return legs
        if not page.get("cursor"):
            break
        path = f"/api/v1/markets?status=open&trading=true&limit=200&cursor={requests.utils.quote(page['cursor'])}"
    return legs


async def run(args):
    config = stx.load_profile()
    key = stx.load_private_key(config)
    rest = Rest(config, key)
    user_id = rest.call("GET", "/api/v1/me")["me"]["user_id"]
    headers = stx.signed_headers(key, config["key_id"], "GET", stx.SOCKET_PATH)
    async with websockets.connect(f"{config['socket_url']}?vsn=2.0.0", additional_headers=headers) as ws:
        player = Player(ws, f"rfq:{user_id}")
        tasks = [asyncio.create_task(player.reader()), asyncio.create_task(player.heartbeat())]
        try:
            status, response = await player.send("phx_join", {})
            if status != "ok":
                sys.exit(f"Join refused: {response.get('reason')}")
            if args.player_id:
                # The pseudonym makers see on this account's requests.
                print(f"Player: {response['external_id']}")
                return 0
            legs = pick_legs(rest)
            if len(legs) < 2:
                sys.exit("Not enough open markets from different events for a combo.")

            status, response = await player.send("request_quote", {"legs": legs, "amount": args.stake})
            if status != "ok":
                sys.exit(f"Request refused: {response.get('reason')}")
            rfq_id = response["rfq_id"]
            print(f"Request: {rfq_id}  stake {args.stake}", flush=True)
            bought = await player.take_best(rfq_id, args.wait, args.gather)
            if not bought:
                await player.send("cancel", {"rfq_id": rfq_id})
                sys.exit("No quote was taken on the buy.")
            combo = bought["market_id"]
            print(f"Bought:  {bought['quantity']} @ {bought['price']}  quote {bought['quote_id']}  combo {combo}",
                  flush=True)

            # The fill reaches the position a moment after it trades.
            for _ in range(15):
                status, out = await player.send("cash_out", {"market_id": combo})
                if status == "ok" or out.get("reason") != "nothing_to_sell":
                    break
                await asyncio.sleep(1)
            if status != "ok":
                sys.exit(f"Cash out refused: {out.get('reason')}")
            print(f"Cash out: {out['rfq_id']}  selling {out['quantity']}", flush=True)
            sold = await player.take_best(out["rfq_id"], args.wait, args.gather)
            if sold:
                print(f"Sold:    {sold['quantity']} @ {sold['price']}  quote {sold['quote_id']}", flush=True)
            else:
                await player.send("cancel", {"rfq_id": out["rfq_id"]})
        finally:
            for task in tasks:
                task.cancel()

    # Nothing left: no position and no resting order on the combo.
    position = "0"
    for _ in range(10):
        page = rest.call("GET", f"/api/v1/positions?market_ids={combo}")
        held = page.get("positions", page.get("data", []))
        position = (held[0].get("position") if held else None) or "0"
        if Decimal(position) == 0:
            break
        time.sleep(1)
    resting = rest.call("GET", f"/api/v1/orders?market_ids={combo}&status=open").get("orders", [])
    for order in resting:
        rest.call("DELETE", f"/api/v1/orders/{order['id']}")
    print(f"Final:   position {position}  open orders {len(resting)}")
    return 0 if sold and Decimal(position) == 0 else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stake", default="1.00", help="dollars, whole cents (default 1.00)")
    parser.add_argument("--wait", type=float, default=30, help="seconds to wait for a first quote (default 30)")
    parser.add_argument("--player-id", action="store_true", help="print this account's external_id and exit")
    parser.add_argument("--gather", type=float, default=3, help="seconds to keep reading after it (default 3)")
    sys.exit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
