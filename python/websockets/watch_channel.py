#!/usr/bin/env python3
"""Join one channel and print every frame it sends.

    python python/websockets/watch_channel.py --topic markets
    python python/websockets/watch_channel.py --topic 'active_orders:<user_id>'
    python python/websockets/watch_channel.py --topic 'market:<market_id or symbol>'

watch.py joins six channels at once and formats the events it recognises. This
joins only what you name and prints frames as they arrive, unformatted, which is
what you want when working through CHANNELS.md one channel at a time.

`--topic` is repeatable, and `<user_id>` is substituted from GET /api/v1/me:

    python python/websockets/watch_channel.py \\
        --topic markets --topic 'portfolio:<user_id>'

`--payload` sets the join payload, which the markets channel uses for
server-side filtering:

    python python/websockets/watch_channel.py --topic markets \\
        --payload '{"rule_filters": ["home_winner"]}'

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
# cancel_on_disconnect ping, which is a separate timer on active_orders; see
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

            while True:
                join_ref, ref, topic, event, payload = json.loads(await ws.recv())
                if topic == "phoenix":          # heartbeat acks, not channel traffic
                    continue
                body = json.dumps(payload, separators=(",", ":"))
                print(f"{stamp()}  {label(topic)} <- {event}  {body[:400]}")
        finally:
            keepalive.cancel()


def main():
    parser = argparse.ArgumentParser(
        description="Join one channel and print every frame it sends."
    )
    parser.add_argument("--profile", help="profile in ~/.stx/credentials")
    parser.add_argument("--topic", action="append", default=[], metavar="TOPIC",
                        required=True,
                        help="topic to join, repeatable. <user_id> is substituted, "
                             "e.g. --topic 'active_orders:<user_id>'")
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
        me = requests.get(config["base_url"] + "/api/v1/me", headers=headers, timeout=15)
        me.raise_for_status()
        user_id = me.json()["me"]["user_id"]
        topics = [topic.replace("<user_id>", user_id) for topic in topics]

    try:
        asyncio.run(watch(config, private_key, topics, join_payload))
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
