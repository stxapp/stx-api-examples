#!/usr/bin/env python3
"""STX leaderboard - read one board, your own standing, and edit your public profile.

    python python/rest/leaderboard.py board                       # weekly volume, all sports, top 10
    python python/rest/leaderboard.py board --metric profit --period all
    python python/rest/leaderboard.py board --category basketball --limit 25
    python python/rest/leaderboard.py me                          # your rank on every board
    python python/rest/leaderboard.py me --category basketball --period monthly
    python python/rest/leaderboard.py profile                     # your handle, avatar, opt-in
    python python/rest/leaderboard.py profile --handle swift.fox12
    python python/rest/leaderboard.py profile --opt-in false      # leave the leaderboard
    python python/rest/leaderboard.py profile --reroll-avatar

Two leaderboard endpoints: GET /api/v1/leaderboard (one board) and
GET /api/v1/leaderboard/me (your standing). ``profile`` uses GET /api/v1/me and
PATCH /api/v1/me/profile.

``--category`` is ``all`` (every sport) or one sport key, the lowercase sport
name as in a row's ``top_sport``. Sport keys are the sports a market has traded
or settled in during the period; any other key is simply an empty board.

``--metric`` and ``--limit`` are always sent. Leave ``metric`` off and the server
picks the operator's opening board, which a client cannot look up. ``limit`` is
clamped to the operator's players per board (at most 100). A board the operator
hides answers with ``shown: false`` and no rows.

Every row on a board is public identity only: a handle, an avatar URL and the
ranked value. No account ids, names or balances are ever returned.

Reads work with a read_only key. ``profile`` with a change needs read_write.
On a deployment where the leaderboard is switched off, every route is a 404.

Requires the packages in python/requirements.txt; ``./install.sh`` puts them in
python/.venv.
"""

import argparse
import os
import random
import sys
from decimal import ROUND_HALF_UP, Decimal
from urllib.parse import urlencode

try:
    import requests
except ModuleNotFoundError as error:
    raise SystemExit(
        f"{error.name} is not installed. Activate the virtualenv ./install.sh made:\n"
        f"  source python/.venv/bin/activate"
    ) from None

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import stx  # noqa: E402

PERIODS = ("daily", "weekly", "monthly", "yearly", "all")
# How each board writes `value`: a dollar string, an integer, or a 0-1 ratio.
MONEY = ("volume", "profit", "biggest_win")
COUNTS = ("predictions", "markets", "streak")
RATIOS = ("win_rate", "return")
METRICS = MONEY + COUNTS + RATIOS
AVATAR_STYLES = ("dots", "rings", "stripes", "grid", "ball", "court", "stitch", "target", "candles", "dice")
AVATAR_PALETTES = ("ember", "forest", "ocean", "grape", "slate", "mint", "rose", "gold")


def request(config, private_key, method, path, body=None):
    """One signed request. The signature covers the path INCLUDING the query string."""
    headers = stx.signed_headers(private_key, config["key_id"], method, path)
    headers["Content-Type"] = "application/json"
    try:
        response = requests.request(method, config["base_url"] + path,
                                    headers=headers, json=body, timeout=15)
    except requests.exceptions.ConnectionError as error:
        sys.exit(stx.unreachable(config["base_url"], error))

    if response.status_code == 404 and "not enabled" in response.text:
        sys.exit("The leaderboard is not enabled on this deployment.")
    if not response.ok:
        sys.exit(f"{method} {path} -> HTTP {response.status_code}: {response.text[:300]}")
    return response.json()


def fmt_value(metric, value):
    """Money boards are dollar strings, counts are integers, ratios are 0-1 numbers."""
    if value is None:
        return "-"
    if metric in COUNTS:
        return f"{value:,}"
    if metric in RATIOS:
        # Half up, as JavaScript's toFixed does, so both examples print the same.
        return f"{Decimal(str(value * 100)).quantize(Decimal('0.1'), ROUND_HALF_UP)}%"
    return stx.fmt_money(value)


# ---------------------------------------------------------------------------
# Leaderboard
# ---------------------------------------------------------------------------


def cmd_board(config, private_key, args):
    """Top rows of one board. No cursor: a board is a fixed top list."""
    query = urlencode({"period": args.period, "category": args.category,
                       "metric": args.metric, "limit": args.limit})
    board = request(config, private_key, "GET", f"/api/v1/leaderboard?{query}")

    print(f"{board['metric']} · {board['period']} · {board['category']}   "
          f"(snapshot {board['refreshed_at']}, resets {board['next_reset_at'] or 'never'})")
    if not board["shown"]:
        print("  this board is hidden by the operator on this deployment")
    elif not board["leaderboard"]:
        print("  nobody is ranked here yet")
    for row in board["leaderboard"]:
        print(f"  {row['rank']:>3}  {row['handle']:<24} {fmt_value(board['metric'], row['value']):>14}"
              f"   {row.get('top_sport') or '-':<12} {config['base_url']}{row['avatar_url']}")


def cmd_me(config, private_key, args):
    """Your own standing on every board, including a rank outside the list."""
    query = urlencode({"period": args.period, "category": args.category})
    me = request(config, private_key, "GET", f"/api/v1/leaderboard/me?{query}")

    print(f"{me['period']} · {me['category']}   listed: {'yes' if me['opted_in'] else 'no'}")
    for metric in METRICS:
        # Win rate is the one board whose rank sits under its own key.
        stat = me["win_rate_rank" if metric == "win_rate" else metric]
        line = f"#{stat['rank']}  {fmt_value(metric, stat['value'])}" if stat else "unranked"
        print(f"  {metric:<12} {line}")
    print(f"  win rate     {fmt_value('win_rate', me['win_rate'])}   settled markets {me['settled_markets']}")


# ---------------------------------------------------------------------------
# Public profile
# ---------------------------------------------------------------------------


def cmd_profile(config, private_key, args):
    """Show the public profile, or change handle / avatar / opt-in with PATCH."""
    body = {}
    if args.handle:
        body["handle"] = args.handle
    if args.opt_in is not None:
        body["leaderboard_opt_in"] = args.opt_in
    if args.reroll_avatar:
        body["avatar"] = {
            "style": random.choice(AVATAR_STYLES),
            "seed": f"{random.getrandbits(32):08x}",
            "palette": random.choice(AVATAR_PALETTES),
        }

    if body:
        # 422 carries the reason: taken, reserved, malformed, or changed within
        # the last 30 days. Sending your current handle again is not a change.
        me = request(config, private_key, "PATCH", "/api/v1/me/profile", body)["me"]
        print("updated")
    else:
        me = request(config, private_key, "GET", "/api/v1/me")["me"]

    # A player who has never opened the leaderboard has no handle or avatar yet.
    avatar = config["base_url"] + me["avatar_url"] if me["avatar_url"] else "-"
    print(f"  handle        {me['handle'] or '-'}")
    print(f"  avatar        {avatar}")
    print(f"  listed        {'yes' if me['leaderboard_opt_in'] else 'no'}")
    print(f"  handle change {'allowed now' if me['handle_changeable_at'] is None else 'after ' + me['handle_changeable_at']}")


def parse_bool(value):
    if value.lower() in ("true", "yes", "1"):
        return True
    if value.lower() in ("false", "no", "0"):
        return False
    raise argparse.ArgumentTypeError("expected true or false")


def parse_limit(value):
    limit = int(value)
    if not 1 <= limit <= 100:
        raise argparse.ArgumentTypeError("expected 1 to 100")
    return limit


COMMANDS = {"board": cmd_board, "me": cmd_me, "profile": cmd_profile}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", nargs="?", default="board", choices=sorted(COMMANDS))
    parser.add_argument("--profile", help="profile in ~/.stx/credentials")
    parser.add_argument("--period", default="weekly", choices=PERIODS)
    parser.add_argument("--category", default="all",
                        help="all, or a sport key such as basketball: a sport a market has traded or settled in")
    parser.add_argument("--metric", default="volume", choices=METRICS)
    parser.add_argument("--limit", type=parse_limit, default=10, help="rows, 1 to 100")
    parser.add_argument("--handle", help="new public handle (3-24 chars, a-z 0-9 . _)")
    parser.add_argument("--opt-in", type=parse_bool, default=None, metavar="true|false",
                        help="show or hide yourself on the leaderboard")
    parser.add_argument("--reroll-avatar", action="store_true", help="pick a new random avatar")
    args = parser.parse_args()

    config = stx.load_profile(args.profile)
    private_key = stx.load_private_key(config)
    print(f"[{config['profile']} -> {config['base_url']}]\n", file=sys.stderr)
    COMMANDS[args.command](config, private_key, args)


if __name__ == "__main__":
    main()
