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
`orders:<user_id>`, and `GET /api/v1/me` is the only place it is
published. Fetch it once at startup and hold it.

## 4. Read market data

```bash
python python/rest/quickstart.py markets
node javascript/rest/quickstart.mjs markets
```

```
SYMBOL                                                BID   OFFER     MAX  TITLE
STXNCAAF-26SEP031900WESKENN-TOTAL46.5               $0.58   $0.72   $1.00  NCAAF - Week 1 WES @
STXNCAAF-26SEP031900ALBBUFF-SPREADBUFFMINUS14.5     $0.56   $0.69   $1.00  NCAAF - Week 1 ALB @
STXNCAAF-26SEP031900ALBBUFF-TOTAL43.5               $0.58   $0.72   $1.00  NCAAF - Week 1 ALB @

200 tradeable of 200 returned by ?status=open&limit=200.
```

The symbol is the whole string, and it is what `--market` takes later. The leg
that distinguishes sibling markets sits at the end, so `TOTAL43.5` and
`TOTAL46.5` differ only in their tail.

`me`, `orders` and `roundtrip` are the other subcommands.

## 5. Place and cancel an order

Needs a `read_write` key. This places a real order, priced ten cents below the
best bid so it rests instead of filling, then cancels it. Note the `price` it
sends is the string `"0.5100"`: a number there is a `400`, and
[Prices](#prices) says why.

```bash
python python/rest/quickstart.py roundtrip
```

```
STXNCAAF-26SEP031900WESKENN-TOTAL46.5  best bid $0.58, max_price $1.00
placing    BUY 1 @ $0.48
placed     <order id>  status=accepted  price=0.4800  filled=0.00
cancelled  status=cancelled
```

That is the full loop: signed request, order accepted, order cancelled. It
refuses to run against a production profile unless you pass
`--force-production`.

## 6. Watch it live

Open a second terminal. The watcher streams the book and the market summary
together with your own orders, fills, positions, settlements and balances, on one
authenticated socket, using the dollar-format topics documented in
[CHANNELS.md](./CHANNELS.md).
Point it at the market `roundtrip` will use so you see both sides of the same
book:

```bash
# terminal 1
python python/websockets/watch.py --market STXNCAAF-26SEP031900WESKENN-TOTAL46.5

# terminal 2
python python/rest/quickstart.py roundtrip
```

![The watcher on the left, a place-and-cancel round trip on the right. The book
goes 5x5 to 6x5 and back as the order rests and is pulled.](./docs/watch-roundtrip.gif)

*Recorded 3 September 2026 against `demo.stxapp.io`. That NCAAF market will have
settled by the time you read this and the prices are whatever was on the book
that afternoon. It is here to show the shape of the two-terminal loop, not
today's data. Re-record it any time with `vhs docs/watch-roundtrip.tape`.*

The same run, written out. Terminal 1 on join, seven channels: the two public
market feeds, plus five private ones scoped to your user id.

```
[default -> https://demo.stxapp.io]
watching STXNCAAF-26SEP031900WESKENN-TOTAL46.5  (7 channels)   ctrl-c to stop

14:51:10  JOIN      BOOK ok, markets=1
14:51:10  JOIN      MARKET ok, sports=['Football'] competitions=['NCAAF']
14:51:10  JOIN      ORDER ok, no market filter
14:51:10  JOIN      FILL ok, no market filter
14:51:10  JOIN      POS ok, no market filter
14:51:10  JOIN      SETTLE ok, no market filter
14:51:10  ORDER     all_orders: 0 row(s)
14:51:10  FILL      all_trades: 0 row(s)
14:51:10  POS       all_positions: 2 row(s)
14:51:10  WALLET    balances  available_balance=9991.8000
```

Neither public topic sends anything on join, so no `BOOK` or `MARKET` row
appears until the market moves. `settlements:` sends no join frame at all, and
`balances:` confirms itself with its `balances` payload rather than a `JOIN`
line, which is why the counts do not line up with the seven joins.

`no market filter` on the four private topics is the server echoing
`selected_market_ids: null`. They accept the same `market_ids` filter
`orderbook` does; the watcher sends none, so everything on the account arrives
whichever market you pointed it at.

The two `JOIN` lines show the halves of the filter contract. `orderbook` takes
`market_ids` and narrows server-side, so only your market ever arrives. `ticker`
takes only `sports` and `competitions`, so the watcher narrows as far as the
server allows and then drops other markets by `market_id` itself. Read the echo
either way: a value the server does not recognise is dropped in silence and
comes back as `null`, meaning no filter at all.

If those joins come back `FAILED ... {'reason': 'unmatched topic'}`, the host is
not running the dollar-format topics yet. The watcher says so once and carries
on; nothing is wrong with your key.

Now run terminal 2:

```
STXNCAAF-26SEP031900WESKENN-TOTAL46.5  best bid $0.58, max_price $1.00
placing    BUY 1 @ $0.48
placed     c8720efd-b5f0-4bb5-9f7f-83a06b714577  status=accepted  price=0.4800  filled=0.00
cancelled  status=cancelled
```

And terminal 1 shows the whole round trip as it happens, order events and book
depth interleaved on the one connection:

```
14:51:19  ORDER     new_open_order  id=c8720efd-...  status=open  action=buy  filled=0.00  quantity=1.00  price=0.4800  client_order_id=quickstart-1788468679
14:51:20  BOOK       517.00 @  $0.58   |   $0.63  @ 810.00    (6x5 levels)
14:51:20  ORDER     new_open_order  id=c8720efd-...  status=cancelled  action=buy  filled=0.00  quantity=1.00  price=0.4800  cancellation_reason=by_player
14:51:20  BOOK       517.00 @  $0.58   |   $0.63  @ 810.00    (5x5 levels)
```

Trimmed a little for width: each `ORDER` row also carries `market_id` and
`rejection_reason`, and the cancellation arrived **twice**. Order events are not
deduplicated, so reconcile on `id` and make your handling idempotent rather than
counting events.

This is the layout to develop against: watcher in one terminal, your strategy in
another. Four things in that output are worth reading closely.

**The order id matches across terminals.** `c8720efd` is the same order the REST
call returned, arriving on the socket a fraction of a second later. That gap is
what step 7 measures.

**The book depth goes 5x5 to 6x5 and back.** Your resting order adds a bid level
while it is open and removes it when cancelled. It sits in the aggregated book
anonymously, like everyone else's, so you see the depth change without seeing
whose it is.

No `MARKET` row appears in that exchange, and it is worth knowing why. `ticker`
fires on price, book top, volume and open interest. This order rests ten cents
under the touch, so it moves none of them - it deepens the book without
changing the best bid. `orderbook` shows it because that feed carries every
level; `ticker` summarises, and by its own definition this is not news. Use
`orderbook` when you need to see your own resting orders, and `ticker` when you
want a market's headline numbers across many markets at once.

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

## Channel examples

The socket carries nine channels: an order book per market, a market-discovery
channel, and six scoped to your user id. Sign the handshake and join whatever
you need on one connection; step 6 above joins six of them at once.

**[CHANNELS.md](./CHANNELS.md)** documents each one: what it is for, the frame
to join it, a real payload, and the client events you can send. It also covers
the two keep-alive timers, `cancel_on_disconnect`, and the reconnect procedure.

To look at a single channel rather than all six, `watch_channel.py` joins only
what you name and prints frames unformatted:

```bash
python python/websockets/watch_channel.py --topic 'portfolio:<user_id>'
```

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

REST `/api/v1` sends **money as a fixed-point decimal string, in dollars**, and
**quantities as strings** too. Nothing there is cents, and nothing there is a
JSON number.

| where | example | what it is |
| --- | --- | --- |
| `market.max_price` | `"1.0000"` | the market's ceiling, $1 on a US market |
| `market.bids[i].price` | `"0.6100"` | $0.61 |
| `market.bids[i].quantity` | `"491.00"` | contracts |
| `market.last_traded_price`, `market.price` | `"0.6100"` | $0.61 |
| `market.volume24h`, `total_volume`, `open_interest` | `"1234.00"` | contracts |
| `order.price` | `"0.5100"` | $0.51, or `null` on a market order |
| `order.quantity`, `order.filled` | `"1.00"` | contracts |
| `order.amount`, `filled_amount`, `total_value`, `avg_price` | `"0.5100"` | dollars |

The decimal count is a **minimum, not a fixed width**. Money carries at least
four places and quantities at least two, but a value keeps any further precision
it genuinely has: an order `price` can carry seven. Parse with a variable-scale
decimal type - Python's `Decimal` - and never with a fixed-width reader.

Not everything numeric is money. `price_change24h` is a percentage and loyalty
`points` are points; both stay plain JSON numbers. Convert what is an amount of
money or a count of contracts, and nothing else.

#### Sending a price

`price` on `POST /api/v1/orders` must be a **string**. A number is rejected:

```
400 price must be a dollar amount as a string, not a number. Values are
dollars, not currency subunits: send "0.56" for 56 cents and "56.00" for
56 dollars (got 5600)
```

That is deliberate. `5600` used to mean $56.00 in cents, and reading it as
$5,600.00 would be a 100x overprice that passes range validation on a $100
market, so the server refuses to guess. `quantity` is exempt and still accepts a
number, because a contract count carries no unit ambiguity.

Any width from zero to seven decimals is accepted - `"0.51"`, `"0.5100"` and
`"0.510000"` are the same order - and the response echoes it at four. Compare
prices as decimals, never as strings.

`python/stx.py` and `javascript/stx.mjs` each expose the two helpers the
examples use: `to_decimal`/`toNumber` to read a field, and
`dollar_string`/`dollarString` to write one.

JavaScript has no decimal type, so the examples parse to `Number`. That is exact
enough for the two-decimal quotes these markets trade at, but it is not a money
type - `0.1 + 0.2` is `0.30000000000000004`. Anything that accumulates, such as
a running P&L or a cost basis, wants a decimal library. Do not skip the parse and
lean on coercion either: `"0.61" - 0.1` happens to give `0.51`, but `"0.61" + 0.1`
is the string `"0.610.1"` and nothing warns you.

#### The ceiling

A market's price ceiling is its own **`max_price`**, not a fixed 99c. US markets
settle at $1, so `max_price` is `"1.0000"` and quotes run $0.01-$0.99. Canadian
markets settle at $100 and `max_price` is `"100.0000"`. Read it off the market.

A price at or above the cap is a `422 The order's price must be lower than 1.00`.

#### The WebSocket topics come in both formats

The **dollar-format topics** - `orderbook`, `ticker`, `trades`, `orders:`,
`fills:`, `positions:`, `settlements:`, `balances:` and `account:` - use exactly
the format above, so a REST snapshot and a socket delta can be mixed without
converting anything. Every example here joins those.

The older topics predate the format and were not converted. They still work, but
they send cents, and one `market:` join reply carries the book twice in two
different units:

| where | example | unit |
| --- | --- | --- |
| socket `ob.b[i].p` | `0.61` | decimal dollars, as a number |
| socket `bids[i].price` | `61` | integer cents |
| `active_orders` push `price` | `51` | integer cents |
| `portfolio` `available_balance` | `1000073` | integer cents |

[CHANNELS.md](./CHANNELS.md) documents both families and maps each legacy topic
onto its replacement. Do not join a topic and its twin on the same socket - you
receive every update twice, once in each format.

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

### Pointing at another host

`STX_EXCHANGE` and `STX_ENVIRONMENT` only choose from the three hosts in the
table. For anything else - a server on your own machine, a review app -
set `STX_BASE_URL`, which wins over the pair:

```sh
STX_BASE_URL=http://localhost:8000 python python/rest/quickstart.py markets
STX_BASE_URL=http://localhost:8000 node javascript/rest/quickstart.mjs markets
```

`http` is handled as well as `https`, and the WebSocket URL follows: the socket
examples connect to `ws://localhost:8000/socket/websocket` without further
configuration.

`./verify` deliberately reads nothing but `STX_DIR` from the environment, so give
it a `base_url` line in the profile instead. The Python and JavaScript examples
read that key too, which is the way to make an override stick:

```ini
[local]
base_url = http://localhost:8000
key_id = <a key registered on that server>
private_key = ~/.stx/local.pem
```

Your key has to exist on whatever host you point at - keys belong to one
environment, so a `demo.stxapp.io` key is not valid against a local server.

**`exchange` and `environment` still apply.** They decide more than the host:
`roundtrip` and the latency examples refuse to place orders when `environment`
is `production`. If you point `base_url` at a real exchange, that guard is all
that stands between an example and a live book, so leave `environment` alone
unless you mean it.

### Known issues

- **`?status=OPEN` returns 400.** Status values are lowercase, and `open` is the
  only accepted one; `?status=suspended` is a 400 as well.
- The `market_updates` channel is documented in some places with the topic
  `market_update`, singular. It is plural.

## Where to go next

| | |
| --- | --- |
| [CHANNELS.md](./CHANNELS.md) | Every channel and event, cancel-on-disconnect, the reconnect procedure |
| [README.md](./README.md) | What is in the repository, and the setup commands |
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
