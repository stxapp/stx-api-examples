# Python examples

Two scripts, no SDK dependency:

| Script | What it does |
| --- | --- |
| `stx_quickstart.py` | Market data, signed reads, and an order round trip |
| `stx_watch.py` | Live watcher - book, your orders, fills, positions and balance |

Set up your API key and `~/.stx/credentials` profile first - see the
[root README](../README.md).

## Setup

Python 3.9 or newer.

**macOS / Linux**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**Windows (PowerShell)**

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If PowerShell blocks the activate script, allow it for this session with
`Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned`, or use
`.\.venv\Scripts\activate.bat` from `cmd.exe`.

On Windows use `py` rather than `python3`, and put credentials in
`%USERPROFILE%\.stx\credentials`. There is no `chmod`, so restrict the folder with:

```powershell
mkdir $env:USERPROFILE\.stx
icacls $env:USERPROFILE\.stx /inheritance:r /grant:r "$($env:USERNAME):(OI)(CI)F"
```

Write `private_key` as a forward-slash path - `C:/Users/you/.stx/ontario-staging.pem`. `~` is
expanded on Windows too.

## Without a key

Market and event discovery need no authentication, so these work immediately:

```bash
python stx_quickstart.py markets    # open markets with best bid / offer
python stx_quickstart.py book       # live order book over the WebSocket
```

```
[profile default -> staging.on.sportsxapp.com]

STXSOC-04JUN226FIFACUP-SPAIN                  46.0 / 52.0     max $100.00  World Cup Winner Spain
STXMLB-26AUG051410TORHOU-SPREADHOUMINUS1.5      -- / --       max $100.00  MLB TOR @ HOU -1.5
```

`book` prints a snapshot, then updates as the book moves:

```
--- update 1  (6 bids / 9 offers)
  bid      46.00 x       36   cum       36
  bid      45.00 x       29   cum       65
  offer    52.00 x        5   cum        5
```

Updates are published only when the book changes, so on a quiet market the snapshot may be all
you get. After 30 idle seconds the script says so and exits rather than hanging.

## With a key

```bash
python stx_quickstart.py orders                              # uses [default]
python stx_quickstart.py --profile ontario-staging orders     # a named profile
python stx_quickstart.py --profile ontario-staging roundtrip
```

Every run prints what it resolved, so you can see you are not pointed at production:

```
[profile ontario-staging -> staging.on.sportsxapp.com]
```

`orders` lists your open orders and needs `read_only`. `roundtrip` needs `read_write` and
places a **real order**: buy 1 contract priced $10 below the best bid so it rests rather than
fills, then cancels it.

```
STXSOC-04JUN226FIFACUP-SPAIN  best bid $46.00 -> bidding $36.00
placed  a6ea08a0-96c2-4722-9afa-fe2bf94b33bd  status=accepted  filled=0
cancel  status=cancelled
```

## Watching sockets in a second console

`stx_watch.py` opens one authenticated socket and streams the book together with your own
orders, fills, positions and balance. Run it beside the commands above to see their effect:

```bash
# console 1
python stx_watch.py --profile ontario-staging --cancel-on-disconnect

# console 2
python stx_quickstart.py --profile ontario-staging roundtrip
```

Console 1 shows the order appear and disappear, and the book depth change with it:

```
20:39:51  JOIN    ORDER ok  {"ping_timeout": 10000, "cancel_on_disconnect": true}
20:39:51  WALLET available_balance=2500000
20:39:51  BOOK       36 @   46.00   |   52.00   @ 5        (6x9 levels)
20:39:57  ORDER  id=a6ea08a0...  status=open       price=3600  filled=0  client_order_id=quickstart-...
20:39:57  BOOK       36 @   46.00   |   52.00   @ 5        (7x9 levels)
20:39:57  ORDER  id=a6ea08a0...  status=cancelled  price=3600  filled=0  client_order_id=quickstart-...
20:39:58  BOOK       36 @   46.00   |   52.00   @ 5        (6x9 levels)
```

Options:

| Flag | Effect |
| --- | --- |
| `--market <market_id>` | Watch a specific market instead of the first with a book |
| `--cancel-on-disconnect` | Ask the exchange to pull flagged orders if this process dies |
| `--ping-timeout <ms>` | Tune that timeout (default 10000) |

Private channels are scoped by user id, so `stx_watch.py` needs `user_id` in the profile.
[SOCKETS.md](../SOCKETS.md) has the full channel reference.

## What these demonstrate

| Command | Auth | Shows |
| --- | --- | --- |
| `markets` | none | `marketInfos`, reading top of book, price units |
| `book` | none | Channel join, `request_snapshot`, `order_book_update`, heartbeats |
| `orders` | read_only | Signing a GraphQL request, `myOrderHistory` |
| `roundtrip` | read_write | `confirmOrder` with `clientOrderId` and `cancelOnDisconnect`, then `cancelOrder` |
| `stx_watch.py` | read_only | Authenticated handshake with `X-` headers, the six private/public channels, cancel-on-disconnect |

Both scripts are commented with the reasoning, not just the calls - `stx_quickstart.py` is the
place to read how signing works.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| `unauthorized` | A header missing, clock skew beyond ±30s, unknown or revoked key, or a bad signature |
| `Operation not supported for <scope> key` | Not available to API keys, or needs `read_write` |
| Handshake fails with HTTP 403 | Bad signature on the socket, or the `X-` prefix is missing |
| `unauthorized` joining a private channel | `user_id` in the profile is not the authenticated user |
| Socket goes quiet after ~20s | Missing heartbeats |
| Prices off by 100x | Socket sends dollars, GraphQL expects cents |
| `price` rejected | Must be an integer number of cents - `6000`, never `60.00` or `"0.60"` |

If a signature will not verify, check in order: the method is uppercase; the path matches
exactly, including any query string and excluding scheme and host; on the WebSocket you signed
`GET` and `/socket/websocket` without `?vsn=`; the base64 is standard and padded; you signed
the message bytes rather than a hash; and the timestamp in the message is byte-for-byte the one
in the header.

## Still stuck?

Ask in Discord - **https://discord.gg/yF9eVzPzNZ**. Include the operation or channel
name, the environment, and the exact error text. [SUPPORT.md](../SUPPORT.md) lists what
helps us answer in one round trip.
