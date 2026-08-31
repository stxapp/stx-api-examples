# STX API examples

Working examples for integrating with the [STX](https://stxapp.io) exchange:
request signing, market data, order placement, live order books over WebSocket,
and a latency measurement you can run against your own connection.

**This is example code, not a client library.** It is deliberately plain and
copyable — each script reads top to bottom and shows what goes on the wire. If
you want a maintained client instead, use **`pysdk`**, the supported STX Python
SDK; ask support for access. The full API reference lives at
[docs.stxapp.io](https://docs.stxapp.io).

```
install.sh  configure  verify       set-up trio - POSIX shell, no Python or Node needed
python/     rest/  websockets/      signed REST, live channels, latency
javascript/ rest/  websockets/
postman/                            REST collection, one request per route
```

Language first, because you already know which language you are writing. Surface
second, because that is the part you get to choose.

## Ten minutes from clone to a live order

```sh
git clone https://github.com/stxapp/stx-api-examples.git
cd stx-api-examples

./install.sh          # sets up only the runtimes you have; skips the rest
./configure           # stores your key id and private key under ~/.stx/
./verify              # signs GET /api/v1/me and prints your user_id and scope
```

`./verify` is the proof that setup worked: it signs a real request with nothing
but `curl` and `openssl`. If it prints a `user_id`, everything else in this
repository will work.

Then pick a language:

#### JavaScript

```sh
node javascript/rest/quickstart.mjs markets      # zero dependencies
```

#### Python

```sh
python python/rest/quickstart.py markets
```

### Generating API key for STX

There are two ways to get one. A key belongs to exactly one environment either
way, and `./configure <profile>` keeps as many as you need side by side.

#### Creating a key in the STX web app

1. Register on the environment you will build against — for the US integration
   exchange that is [demo.stxapp.io](https://demo.stxapp.io).
2. **Account → API Keys → Create API Key.** Choose a scope: `read_only`, or
   `read_write` to place and cancel orders.
3. Copy the **key id** and the **private key PEM**. The matching public key is
   generated for you and stored on the server. The private key is shown once and
   is never stored by the exchange. Lose it and you revoke the key and issue
   another.

Then run `./configure` and give it that **key id**. It takes the private key
either as a path to a `.pem` file or pasted in directly; paste the PEM you just
copied, since the exchange will not show it again.

#### Generating your own key pair and registering the public key

Keys are Ed25519. Instead of letting the exchange generate the pair, you can
generate it locally and hand over only the public half, so the private key never
leaves your machine:

```sh
# Generates the PRIVATE key and writes it to the file. Keep it; this is what
# signs your requests. Note that -out overwrites without asking.
openssl genpkey -algorithm ed25519 -out ~/.stx/default.pem

# Derives the PUBLIC key from that file and prints it. Writes nothing and
# creates no second key. Paste this when creating the key on the exchange.
openssl pkey -in ~/.stx/default.pem -pubout
```

Paste that public key into **Account → API Keys → Create API Key** in place of
letting the exchange generate one. You still get a **key id** back; pair it with
your local PEM in `./configure`.

## Quickstart Examples

### Shell: set up credentials and prove signing works

Needs neither Python nor Node — just `curl` and `openssl`. On macOS the system
`openssl` is LibreSSL, which cannot sign with Ed25519; `brew install openssl@3`
and put it first on `PATH`.

| | |
| --- | --- |
| `./install.sh` | Detects which runtimes are present and sets up only those, inside this directory. Nothing is installed globally. Prints what it set up and what it skipped. |
| `./configure [profile]` | Prompts for a key id and a private key — from a `.pem` you point at, or pasted with echo off — and writes `~/.stx/credentials` plus `~/.stx/<profile>.pem`, `chmod 700` on the directory and `600` on the files. Asks before replacing a profile. Never takes key material as an argument, and never prints your private key. |
| `./verify [profile]` | Signs `GET /api/v1/me` with `curl` and `openssl`, prints your `user_id` and `scope`. A complete signing example in 30 lines of shell. |

Run in that order, the three answer three separate questions: whether your
machine has the runtimes and can install the dependencies, whether your key id
and private key are stored where every example looks for them, and whether the
exchange accepts a signature built from them. They are three scripts rather than
one so that a failure tells you which of the three is wrong.

### Python: signed REST, live channels, latency

```sh
python python/rest/quickstart.py me | markets | orders | roundtrip
python python/websockets/watch.py [--market <id>] [--cancel-on-disconnect]
python python/websockets/latency.py [--rounds 10]
```

`python/stx.py` holds the host table, the profile loader, the signing function
and the price-unit conversion; the three scripts are the examples. Dependencies
are pinned in `python/requirements.txt` — `cryptography` is not optional,
because Python has no Ed25519 in its standard library.

`watch.py` speaks the raw Phoenix channel protocol, so you can see the frames.

### JavaScript: signed REST, live channels, latency

```sh
node javascript/rest/quickstart.mjs me | markets | orders | roundtrip
node javascript/websockets/watch.mjs [--market <id>] [--cancel-on-disconnect]
node javascript/websockets/latency.mjs [--rounds 10]
```

**The REST examples have zero dependencies.** Node has Ed25519 in
`node:crypto` and `fetch` built in, so they run on a bare Node 20+ with nothing
installed. The WebSocket examples use `phoenix` — the exchange's own channel
client, which handles `join_ref` bookkeeping, the socket heartbeat and rejoining
after a reconnect — and `ws`, because Node's built-in `WebSocket` cannot set the
handshake headers the signature travels in. Both are pinned and
`package-lock.json` is committed: `npm ci`.

### Latency: the same measurement in both runtimes

`python/websockets/latency.py` and `javascript/websockets/latency.mjs` are the
same measurement in two runtimes — place an order over REST, wait for the order
book push that reflects it, cancel, wait again — so you can compare runtime and
library overhead on your own network path before choosing one.

They place **real orders**, priced to rest rather than fill, and refuse a
production profile unless `--force-production` is passed.

## How the API is shaped

Two transports, and a serious integration uses both.

- **REST** at `/api/v1` — orders, market and event discovery, your own trades,
  positions and settlements. This is the documented surface.
- **WebSocket** at `/socket/websocket` — Phoenix channels. Order book depth,
  fills, order state changes, positions, balance. The only way to know about a
  fill promptly.

REST answers what was true when you asked. Poll it for state you can afford to
be stale about, stream everything else, and reconcile the two on a slow cadence
— a dropped socket message on a live connection is silent.

### Signing

Three headers on every call. There are no public REST endpoints, so this is
every request you will ever make:

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
- Plain Ed25519 (RFC 8032) over the UTF-8 bytes — **not** `Ed25519ph`, no
  pre-hashing — base64 with the standard alphabet and padding, not URL-safe.
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

Arithmetic against the touch has to convert first: subtracting from the raw book
value is a `TypeError` in Python and, worse, a silent `-9.46` in JavaScript.
`python/stx.py` and `javascript/stx.mjs` each expose one helper for it,
`book_price_cents` and `bookPriceCents`, and every example goes through it.

A market's price ceiling is its own **`max_price`**, not a fixed 99. US markets
settle at $1, so `max_price` is 100 and quotes run 1–99. Canadian markets settle
at $100 and `max_price` is 10000. Read it off the market.

The rejection message for a price over the cap reports it in **dollars** while
the field is in **cents**: `price: 4650` on a $1 market returns
`422 The order's price must be lower than 1.00`.

### Response shapes

- A collection is `{cursor, <resource>: [...]}` — `{cursor, orders: [...]}`, not
  `{data: [...]}`. Feed `cursor` back as `?cursor=...`; it is `null` on the last
  page.
- `POST /api/v1/orders` returns **200**, not 201, as `{"order": {...}}`.
- The order body is **flat**. Wrapping it in `{"user_order": {...}}` returns 400.

### Credentials

`./configure` writes `~/.stx/credentials`, an INI file with one section per
profile:

```ini
[default]
exchange = us
environment = integration
key_id = <your key id>
private_key = /home/you/.stx/default.pem
```

Hostnames are deliberately absent: `exchange` and `environment` are resolved
through a single table per language — `python/stx.py`, `javascript/stx.mjs`, and
the `base_url()` function in `verify` — so no script contains a hostname.

| exchange | environment | |
| --- | --- | --- |
| `us` | `integration` | `demo.stxapp.io` |
| `ca` | `integration` | `api-staging.on.sportsxapp.com` |
| `ca` | `production` | `api.on.stxapp.ca` — real money |

Any value can be overridden by an environment variable, which is what you want
in CI: `STX_PROFILE`, `STX_EXCHANGE`, `STX_ENVIRONMENT`, `STX_KEY_ID`,
`STX_PRIVATE_KEY`.

### Your user id

Private WebSocket topics are scoped by user id — `active_orders:<user_id>`,
`portfolio:<user_id>` — and `GET /api/v1/me` is the only place it is published.
Fetch it once at startup and hold it; `./verify` prints it too.

### Two socket timers, not one

The socket examples get this right, and it is the single easiest thing to get
wrong by hand:

| Timer | Reset by | Window | Miss it and |
| --- | --- | --- | --- |
| Socket keep-alive | a heartbeat on the `phoenix` topic | 60s | the connection closes |
| `cancel_on_disconnect` | a `ping` on the `active_orders` topic | 5000–20000 ms, whatever you negotiated at join | **your flagged orders are cancelled** on a connection that is still up |

A 30-second heartbeat keeps the socket alive and still blows the `ping`
deadline. Send the channel `ping` on its own timer, not in response to traffic —
a quiet market produces no traffic and the deadline does not care.

## Known issues

Worth knowing before you hit them:

- **`?status=OPEN` returns 400.** Status values are lowercase, and `open` is the
  only accepted one — `?status=suspended` is a 400 as well.
- The `market_updates` channel is documented in some places with the topic
  `market_update`, singular. It is plural.

## Also in this repository

`GETTING_STARTED.md` and `SOCKETS.md` predate the REST API and describe the
exchange as GraphQL-only, on hostnames we no longer publish. They are being replaced by [docs.stxapp.io](https://docs.stxapp.io) and
are kept here only until that move completes. **Where they disagree with this
file or with the docs site, they are wrong.**

## Support

Questions are welcome in our Discord, where the answers are visible to everyone
building on the exchange and you are talking to the engineers who build it:

**https://discord.gg/yF9eVzPzNZ**

See [Support](https://docs.stxapp.io/support/) for what to include when asking about a failing
request, and for the shared-Slack option on larger integrations.

There are no enforced rate limits today. Prefer the batch cancel endpoints over
loops, and tell us your expected message rates so we watch the right things as
you scale.

## License

MIT — see [LICENSE](./LICENSE).
