#!/usr/bin/env python3
"""Join one channel and print every frame it sends.

    python python/websockets/watch_channel.py --topic ticker
    python python/websockets/watch_channel.py --topic 'orders:<user_id>'
    python python/websockets/watch_channel.py --topic 'account:<user_id>'

watch.py joins six channels at once and formats the events it recognises. This
joins only what you name and prints frames as they arrive, unformatted, which is
what you want when working through CHANNELS.md one channel at a time.

`--topic` is repeatable, and `<user_id>` is substituted from GET /api/v1/me:

    python python/websockets/watch_channel.py \\
        --topic ticker --topic 'balances:<user_id>'

`--payload` sets the join payload. The public topics use it for filtering, and
`orderbook` REQUIRES at least one market_id:

    python python/websockets/watch_channel.py --topic orderbook \\
        --payload '{"market_ids": ["<market_id>"]}'

    python python/websockets/watch_channel.py --topic ticker \\
        --payload '{"sports": ["baseball"], "competitions": ["MLB"]}'

`account:<user_id>` is the one topic with no legacy twin: it carries orders,
fills, positions, settlements and balances on a single join. Do not join it
alongside the per-type topics - you would receive everything twice.

Requires the packages in python/requirements.txt; ``./install.sh`` puts them in
python/.venv.
"""

import argparse
import asyncio
import json
import os
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

# The socket closes after 60s of silence. This is the socket keep-alive, not the
# cancel_on_disconnect ping, which is a separate timer on orders:; see
# CHANNELS.md. watch.py is the example that negotiates that one.
HEARTBEAT_SECONDS = 20


def stamp():
    return time.strftime("%H:%M:%S")


def label(topic):
    """The topic's prefix, padded. The full topic is on the join frame above;
    repeating a user id on every line only makes them unreadable."""
    return f"{topic.split(':')[0]:<18}"


async def heartbeat(ws):
    """`[null, ref, "phoenix", "heartbeat", {}]`, on its own timer."""
    ref = 0
    while True:
        await asyncio.sleep(HEARTBEAT_SECONDS)
        ref += 1
        await ws.send(json.dumps([None, str(ref), "phoenix", "heartbeat", {}]))


async def watch(config, private_key, topics, join_payload):
    # The handshake signs GET against /socket/websocket with the query string
    # DROPPED - `?vsn=2.0.0` is on the URL but not in the signed message.
    headers = stx.signed_headers(private_key, config["key_id"], "GET", stx.SOCKET_PATH)

    async with websockets.connect(
        f"{config['socket_url']}?vsn=2.0.0", additional_headers=headers
    ) as ws:
        keepalive = asyncio.create_task(heartbeat(ws))
        try:
            for index, topic in enumerate(topics):
                # [join_ref, ref, topic, event, payload]. Every later message to
                # this topic must repeat the same join_ref.
                frame = [str(index), str(index), topic, "phx_join", join_payload]
                await ws.send(json.dumps(frame, separators=(",", ":")))
                # Echo the frame we just sent. It is the whole point of this
                # script: what CHANNELS.md documents is literally what goes out.
                print(f"{stamp()}  {label(topic)} -> "
                      f"{json.dumps(frame, separators=(',', ':'))}")
            print()

            warned = {"unmatched": False}

            while True:
                join_ref, ref, topic, event, payload = json.loads(await ws.recv())
                if topic == "phoenix":          # heartbeat acks, not channel traffic
                    continue
                body = json.dumps(payload, separators=(",", ":"))
                print(f"{stamp()}  {label(topic)} <- {event}  {body[:400]}")

                # The raw frame above is the point of this script, so the reason
                # is printed beside it rather than instead of it. Once per run:
                # on an older host every dollar topic fails the same way.
                if not warned["unmatched"] and \
                        (payload.get("response") or {}).get("reason") == "unmatched topic":
                    print(UNMATCHED_TOPIC_HINT, file=sys.stderr)
                    warned["unmatched"] = True
        finally:
            keepalive.cancel()


# `unmatched topic` is the server saying it has never heard of the topic, which
# on a correct client means the host predates it - or that the topic is
# misspelled, which looks identical. Both are worth naming in a tool whose whole
# job is trying one channel at a time.
UNMATCHED_TOPIC_HINT = (
    "\n  'unmatched topic' means the server does not know that topic.\n"
    "  Check the spelling against CHANNELS.md - and note that the dollar-format\n"
    "  topics need a host running them, while older deployments carry only the\n"
    "  legacy cents topics.\n"
)


def main():
    parser = argparse.ArgumentParser(
        description="Join one channel and print every frame it sends."
    )
    parser.add_argument("--profile", help="profile in ~/.stx/credentials")
    parser.add_argument("--topic", action="append", default=[], metavar="TOPIC",
                        required=True,
                        help="topic to join, repeatable. <user_id> is substituted, "
                             "e.g. --topic 'orders:<user_id>'")
    parser.add_argument("--payload", default="{}", metavar="JSON",
                        help="join payload, e.g. --payload "
                             "'{\"rule_filters\": [\"home_winner\"]}' on the markets "
                             "channel. Default {}")
    args = parser.parse_args()

    try:
        join_payload = json.loads(args.payload)
    except json.JSONDecodeError as error:
        sys.exit(f"--payload is not valid JSON: {error}")
    if not isinstance(join_payload, dict):
        sys.exit("--payload must be a JSON object")

    config = stx.load_profile(args.profile)
    private_key = stx.load_private_key(config)
    print(f"[{config['profile']} -> {config['base_url']}]", file=sys.stderr)

    topics = args.topic
    if any("<user_id>" in topic for topic in topics):
        # Only fetch it when a topic actually needs it, so the public market
        # channels work without the extra round trip.
        headers = stx.signed_headers(private_key, config["key_id"], "GET", "/api/v1/me")
        try:
            me = requests.get(config["base_url"] + "/api/v1/me", headers=headers, timeout=15)
        except requests.exceptions.ConnectionError as error:
            sys.exit(stx.unreachable(config["base_url"], error))
        me.raise_for_status()
        user_id = me.json()["me"]["user_id"]
        topics = [topic.replace("<user_id>", user_id) for topic in topics]

    try:
        asyncio.run(watch(config, private_key, topics, join_payload))
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
