# Getting started

From clone to a live order in about ten minutes, one step at a time. If you
would rather read a reference than a walkthrough, [README.md](./README.md)
covers the same ground more densely, and the full API lives at
[docs.stxapp.io](https://docs.stxapp.io).

Every command below has been run end to end against the US integration
exchange, `demo.stxapp.io`.

## 1. Clone and install

Python 3.9 or newer, or Node 20 or newer. You do not need both.

```bash
git clone https://github.com/stxapp/stx-api-examples.git
cd stx-api-examples
./install.sh
```

`install.sh` sets up only the runtimes you already have and skips the rest.
Nothing is installed globally: Python gets a virtualenv at `python/.venv` with
the three pinned dependencies, and Node gets `javascript/node_modules` from the
committed lockfile.

```
Python  : python3 3.13.4
          creating python/.venv
          installing python/requirements.txt
Node    : node v22.14.0
          installing javascript/package-lock.json
```

For the Python examples, activate the virtualenv it made:

```bash
source python/.venv/bin/activate   # Windows: python\.venv\Scripts\Activate.ps1
```

The JavaScript REST examples need nothing installed at all; Node has Ed25519 in
`node:crypto` and `fetch` built in. Only the WebSocket examples use packages.

## 2. Create an API key

Every `/api/v1` route requires a signature, so you need a key before you can
read even market data.

1. Register on the environment you will build against. For the US integration
   exchange that is [demo.stxapp.io](https://demo.stxapp.io).
2. **Account -> API Keys -> Create API Key.** Choose `read_only`, or
   `read_write` if you want to place orders in step 5.
3. Copy the **key id** and the **private key PEM**. The matching public key is
   generated for you and stored on the server. The private key is shown once
   and is never stored by the exchange, so save it before closing the dialog.

You can also generate the pair yourself and hand over only the public half, so
the private key never leaves your machine. Keys are Ed25519:

```sh
# Generates the PRIVATE key and writes it to the file. Keep it; this is what
# signs your requests. Note that -out overwrites without asking.
openssl genpkey -algorithm ed25519 -out ~/.stx/default.pem

# Derives the PUBLIC key from that file and prints it. Writes nothing and
# creates no second key. Paste this when creating the key on the exchange.
openssl pkey -in ~/.stx/default.pem -pubout
```

Paste that public key into **Account -> API Keys -> Create API Key** in place of
letting the exchange generate one. You still get a **key id** back; pair it with
your local PEM in `./configure`.

## 3. Store the credentials and prove they sign

`./configure` writes the profile for you. It asks for a key id and takes the
private key either as a path to a `.pem` file or pasted in with echo off, then
writes `~/.stx/credentials` and `~/.stx/<profile>.pem` with `700` on the
directory and `600` on the files. It never takes key material as a command-line
argument, and never prints your private key.

```bash
./configure          # writes the [default] profile
```

The file it produces is an INI with one section per profile:

```ini
[default]
exchange    = us
environment = integration
key_id      = <your key id>
private_key = /Users/you/.stx/default.pem
```

Hostnames are deliberately absent. `exchange` and `environment` resolve through
one table per language:

| exchange | environment | host |
| --- | --- | --- |
| `us` | `integration` | `demo.stxapp.io` |
| `ca` | `integration` | `api-staging.on.sportsxapp.com` |
| `ca` | `production` | `api.on.stxapp.ca` (real money) |

`./configure ca-integration` writes a second profile alongside the first; every
script takes `--profile <name>` to pick one.

Now confirm the whole chain works:

```bash
./verify
```

```
profile     [default] -> us/integration
host        https://demo.stxapp.io
signing     GET /api/v1/me

OK
  user_id   <your user id>
  scope     read_write
```

`verify` uses only `curl` and `openssl`, so it works before you have installed
anything, and it is a complete signing example in about thirty lines of shell.
If it prints a `user_id`, everything else in this repository will work.

If you get `unauthorized` instead, it is almost always clock skew: the
timestamp must be within 30 seconds of the server clock.

Note the `user_id` it prints. Private WebSocket topics are scoped by it, as in
`active_orders:<user_id>`, and `GET /api/v1/me` is the only place it is
published. Fetch it once at startup and hold it.

## 4. Read market data

```bash
python python/rest/quickstart.py markets
node javascript/rest/quickstart.mjs markets
```

```
SYMBOL                                        BID   OFFER     MAX  TITLE
STXMLB-26AUG311805SFATL-GAMEATL               61c     75c    100c  MLB SF @ ATL
STXMLB-26AUG311805SFATL-GAMESF                34c     45c    100c  MLB SF @ ATL
STXEPL-26AUG311500ARSAVL-TOTAL2.5             53c     66c    100c  EPL ARS @ AVL OU 2.5

200 tradeable of 200 returned by ?status=open&limit=200.
```

The symbol is the whole string, and it is what `--market` takes later. The leg
that distinguishes sibling markets sits at the end, so `TOTAL2.5` and
`TOTAL3.5` differ only in their tail.

`me`, `orders` and `roundtrip` are the other subcommands.

## 5. Place and cancel an order

Needs a `read_write` key. This places a real order, priced ten cents below the
best bid so it rests instead of filling, then cancels it:

```bash
python python/rest/quickstart.py roundtrip
```

```
STXMLB-26AUG311805SFATL-GAMEATL  best bid 61c, max_price 100c
placing    BUY 1 @ 51c
placed     <order id>  status=accepted  filled=0
cancelled  status=cancelled
```

That is the full loop: signed request, order accepted, order cancelled. It
refuses to run against a production profile unless you pass
`--force-production`.

## 6. Watch it live

Open a second terminal. The watcher streams the book together with your own
orders, fills, positions, settlements and balance, on one authenticated socket.
Point it at the market `roundtrip` will use so you see both sides of the same
book:

```bash
# terminal 1
python python/websockets/watch.py --market STXMLB-26AUG311805SFATL-GAMEATL

# terminal 2
python python/rest/quickstart.py roundtrip
```

![The watcher on the left, a place-and-cancel round trip on the right. The book
goes 5x5 to 6x5 and back as the order rests and is pulled.](./docs/watch-roundtrip.gif)

*Recorded 31 August 2026 against `demo.stxapp.io`. That MLB market has long since
settled and the prices are whatever was on the book that afternoon. It is here to
show the shape of the two-terminal loop, not today's data. Re-record it any time
with `vhs docs/watch-roundtrip.tape`.*

The same run, written out. Terminal 1 on join, six channels: the market book,
plus five private ones scoped to your user id.

```
[default -> https://demo.stxapp.io]
watching STXMLB-26AUG311805SFATL-GAMEATL  (6 channels)   ctrl-c to stop

14:31:11  BOOK    491.0 @   61c   |   66c   @ 882.0    (5x5 levels)
14:31:11  ORDER   all_orders: 0 row(s)
14:31:11  TRADE   all_trades: 0 row(s)
14:31:11  POS     all_positions: 3 row(s)
14:31:11  WALLET  summary  available_balance=1000073
```

Now run terminal 2:

```
STXMLB-26AUG311805SFATL-GAMEATL  best bid 61c, max_price 100c
placing    BUY 1 @ 51c
placed     ff7217e7-9a77-406a-8a57-87a18d4d9f82  status=accepted  filled=0
cancelled  status=cancelled
```

And terminal 1 shows the whole round trip as it happens, order events and book
depth interleaved on the one connection:

```
14:31:14  ORDER   new_open_order  id=ff7217e7-...  status=open       price=51  client_order_id=quickstart-1788208273
14:31:14  BOOK    491.0 @   61c   |   66c   @ 882.0    (6x5 levels)
14:31:14  ORDER   new_open_order  id=ff7217e7-...  status=cancelled  price=51  client_order_id=quickstart-1788208273  cancellation_reason=by_player
14:31:14  BOOK    491.0 @   61c   |   66c   @ 882.0    (5x5 levels)
```

This is the layout to develop against: watcher in one terminal, your strategy in
another. Four things in that output are worth reading closely.

**The order id matches across terminals.** `ff7217e7` is the same order the REST
call returned, arriving on the socket a fraction of a second later. That gap is
what step 7 measures.

**The book depth goes 5x5 to 6x5 and back.** Your resting order adds a bid level
while it is open and removes it when cancelled. It sits in the aggregated book
anonymously, like everyone else's, so you see the depth change without seeing
whose it is.

**Your `client_order_id` comes back on every order event.** Set it when you
place, and you can reconcile against your own book without storing our ids.

**Order events arrive on your private channel, not the market channel.** They
are scoped by user id, so you see your own orders even when the watcher is
pointed at a different market. Only the `BOOK` lines are specific to the market
you joined.

`watch.py` speaks the raw Phoenix protocol, so you can see the frames;
`watch.mjs` uses the `phoenix` client instead. Add `--cancel-on-disconnect` and
the exchange cancels your flagged orders if the process dies, which is what you
want in anything that quotes.

## 7. Measure the round trip

```bash
python python/websockets/latency.py
node javascript/websockets/latency.mjs
```

Each round places a resting order and waits for the order book push that
reflects it, then cancels and waits again, timing the HTTP call and the socket
push separately. The two scripts are the same measurement in two runtimes, so
you can compare them on your own network path. They place real orders, so
`--rounds` is capped at 10.

## Reference

Everything below is the detail you will want once the walkthrough works. The
full API lives at [docs.stxapp.io](https://docs.stxapp.io).

### How the API is shaped

Two transports, and a serious integration uses both.

- **REST** at `/api/v1`: orders, market and event discovery, your own trades,
  positions and settlements. This is the documented surface.
- **WebSocket** at `/socket/websocket`: Phoenix channels. Order book depth,
  fills, order state changes, positions, balance. The only way to know about a
  fill promptly.

REST answers what was true when you asked. Poll it for state you can afford to
be stale about, stream everything else, and reconcile the two on a slow cadence.
A dropped socket message on a live connection is silent.

### Signing

Three headers on every call. Every route needs them, so this is every request
you will ever make:

| Header | Value |
| --- | --- |
| `X-STX-ACCESS-KEY` | your key id |
| `X-STX-ACCESS-TIMESTAMP` | Unix time in **milliseconds**, as a string |
| `X-STX-ACCESS-SIGNATURE` | base64 Ed25519 signature of the message below |

The message is a bare concatenation, with no separators:

```
timestamp_ms + HTTP_METHOD_UPPERCASE + path
```

- **The body is not signed.** Only the method and the path.
- The path **includes its query string** exactly as sent, and never the scheme or
  host. Signing `/api/v1/markets` and sending `/api/v1/markets?status=open` is a
  401.
- Plain Ed25519 (RFC 8032) over the UTF-8 bytes, **not** `Ed25519ph` and no
  pre-hashing. Base64 with the standard alphabet and padding, not URL-safe.
- The timestamp must be within **30 seconds** of the server clock. Generate it
  per request and keep the machine on NTP; a clock a minute fast fails every
  request with a 401 that looks exactly like a bad key.

The WebSocket handshake signs the same way, with one difference: the method is
`GET` and the path is `/socket/websocket` with **any query string dropped**, even
though `?vsn=2.0.0` is on the URL.

### Prices

Two representations, and they do not agree. Orders are integer cents. Book and
quote prices are decimal dollars, sent as strings.

| where | example | unit |
| --- | --- | --- |
| `market.bids[i].price`, socket `level.p` | `"0.54"` | decimal dollars, as a string |
| `market.max_price` | `100` | integer cents |
| `order.price`, on POST and on GET | `54` | integer cents |

This means a price read off the book cannot go straight into an order. Say the
best bid is `"0.54"` and you want to rest an order ten cents below it, at 44c.
Subtracting from that string raises a `TypeError` in Python. JavaScript is worse,
because it coerces instead of complaining: `"0.54" - 10` is `-9.46`, which clamps
to a 1c order rather than the 44c one you meant, and nothing tells you.

Convert to cents first. `python/stx.py` and `javascript/stx.mjs` each expose one
helper for exactly this, `book_price_cents` and `bookPriceCents`, and every
example here goes through it.

A market's price ceiling is its own **`max_price`**, not a fixed 99. US markets
settle at $1, so `max_price` is 100 and quotes run 1-99. Canadian markets settle
at $100 and `max_price` is 10000. Read it off the market.

The rejection message for a price over the cap reports it in **dollars** while
the field is in **cents**: `price: 4650` on a $1 market returns
`422 The order's price must be lower than 1.00`.

### Response shapes

- A collection is `{cursor, <resource>: [...]}`, so `{cursor, orders: [...]}`, not
  `{data: [...]}`. Feed `cursor` back as `?cursor=...`; it is `null` on the last
  page.
- `POST /api/v1/orders` returns **200**, not 201, as `{"order": {...}}`.
- The order body is **flat**. Wrapping it in `{"user_order": {...}}` returns 400
  with `{"error":"market_id is required"}`, which points at the wrong problem:
  the field is there, just one level down.

### Environment variables

Environment variables override `~/.stx/credentials`. Two of them are enough to
run the Python and JavaScript examples with no credentials file at all:

```sh
STX_KEY_ID=<your key id> STX_PRIVATE_KEY=~/.stx/default.pem \
  python python/rest/quickstart.py me
```

`STX_PRIVATE_KEY` is the path to the PEM, not its contents. The other three are
optional and only override what the host table resolves: `STX_PROFILE`,
`STX_EXCHANGE` and `STX_ENVIRONMENT`, defaulting to `default`, `us` and
`integration`.

`./configure` and `./verify` read only `STX_DIR` and take everything else from
the file, so `./verify` ignores a `STX_KEY_ID` you set for the examples.

### Sockets

There are two timers, not one, and it is the single easiest thing to get wrong
by hand:

| Timer | Reset by | Window | Miss it and |
| --- | --- | --- | --- |
| Socket keep-alive | a heartbeat on the `phoenix` topic | 60s | the connection closes |
| `cancel_on_disconnect` | a `ping` on the `active_orders` topic | 5000-20000 ms, whatever you negotiated at join | **your flagged orders are cancelled** on a connection that is still up |

A 30-second heartbeat keeps the socket alive and still blows the `ping`
deadline. Send the channel `ping` on its own timer, not in response to traffic:
a quiet market produces no traffic and the deadline does not care.

**Reuse the `join_ref`.** Phoenix frames are `[join_ref, ref, topic, event,
payload]`, and a message to a topic must carry the same `join_ref` you used to
join it, or the server ignores it silently. This is the usual reason
`request_snapshot` appears to do nothing.

**A quiet market publishes nothing.** Book updates are emitted when the book
changes, so silence is not a broken connection. Use `request_snapshot` after any
reconnect rather than waiting for a tick.

### Known issues

- **`?status=OPEN` returns 400.** Status values are lowercase, and `open` is the
  only accepted one; `?status=suspended` is a 400 as well.
- The `market_updates` channel is documented in some places with the topic
  `market_update`, singular. It is plural.

## Where to go next

| | |
| --- | --- |
| [README.md](./README.md) | The reference: signing, price units, response shapes, known issues |
| [SOCKETS.md](./SOCKETS.md) | Every channel and event, cancel-on-disconnect, reconnect procedure |
| [Authentication](https://docs.stxapp.io/api/authentication/) | Signing in several languages, with a test vector |
| [postman/](./postman) | One request per route, with the signing one-liner |

For a market-making loop specifically: tag every order with your own
`client_order_id`, use `cancel_on_disconnect` so a dropped connection does not
leave you quoting, re-quote with a cancel followed by a place since there is no
atomic replace, and take fills from the `active_trades` channel rather than
polling. There are no enforced rate limits today, but prefer the batch cancel
endpoints over loops.

Stuck on any step above? Ask in Discord, **https://discord.gg/yF9eVzPzNZ**,
where you will be talking to the engineers who build the exchange.
[Support](https://docs.stxapp.io/support/) lists what to include so we can
answer in one round trip.

See [Known issues](#known-issues) above for the handful of rough edges we know
about, so none of them costs you an afternoon.
