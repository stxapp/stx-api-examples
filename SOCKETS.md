# STX WebSockets

Everything real-time arrives over one WebSocket connection. This document covers the
protocol, the channels, and the behaviours that are easy to get wrong.

`stx_watch.py` in this repo is a working implementation of everything described here.

## Connecting

```
wss://staging.on.sportsxapp.com/socket/websocket?vsn=2.0.0
```

One connection carries as many channels as you need - book updates for several markets plus
your own order and trade streams all multiplex over a single socket. Open one, not one per
market.

**Anonymous** is fine for market data. Public channels need no credentials at all.

**Authenticated** connections sign the handshake, using the same Ed25519 key as the GraphQL
API with two differences:

| | GraphQL | WebSocket |
| --- | --- | --- |
| Header names | `STX-ACCESS-KEY`, ... | `X-STX-ACCESS-KEY`, ... (**`X-` prefix**) |
| Signed message | `<ts>POST/api/graphql` | `<ts>GET/socket/websocket` |

The `X-` prefix is required because the socket transport only surfaces `x-*` headers. The
query string is **not** part of the signed path - sign `/socket/websocket` even though you
connect with `?vsn=2.0.0`. A bad signature is rejected at the handshake with HTTP 403, so
failures surface as a connection error rather than a channel error.

API keys do not expire, so an authenticated socket never needs reconnecting for token
refresh.

## Frame format

Phoenix channel protocol v2. Every frame is a five-element array:

```
[join_ref, ref, topic, event, payload]
```

- `join_ref` - the ref you used when joining that topic. **Subsequent messages to a topic
  must reuse its `join_ref`**, or the server ignores them. This is the most common reason a
  `request_snapshot` or `ping` appears to do nothing.
- `ref` - your correlation id for this message; replies echo it.
- `topic` - e.g. `market:<market_id>`.
- `event` - `phx_join`, `phx_reply`, or an application event.
- `payload` - a JSON object.

Joining:

```json
["0", "0", "market:71692895-2991-440b-923d-7dfb6783cb17", "phx_join", {}]
```

The reply comes back as `phx_reply` with `{"status": "ok", "response": {...}}`.

## Keep the connection alive

Sockets close after roughly **20 seconds** of silence. Send a heartbeat every 15 seconds:

```json
[null, "1", "phoenix", "heartbeat", {}]
```

Without it the server closes the connection and, from the client side, it simply looks like
updates stopped. This is separate from the `ping` used by cancel-on-disconnect below.

## Public channels

### `market:<market_id>` - order book for one market

The channel to use if you are pricing or quoting.

**`order_book_update`** - aggregated depth, published roughly every 200 ms while the book is
changing. `b` (bids) and `o` (offers) are in fill-priority order: highest bid first, cheapest
offer first.

```json
{
  "ob": {
    "b": [{ "p": 49.0, "q": 8.0,  "l": 392.0,  "tc": 8.0,  "tl": 392.0 },
          { "p": 46.0, "q": 12.0, "l": 552.0,  "tc": 20.0, "tl": 944.0 }],
    "o": [{ "p": 50.0, "q": 39.0, "l": 1950.0, "tc": 39.0, "tl": 1950.0 }]
  }
}
```

| Key | Meaning |
| --- | --- |
| `p` | Price for this level |
| `q` | Contracts remaining at this level |
| `l` | Liquidity at this level, `q x p` |
| `tc` | Cumulative contracts, this level and all better |
| `tl` | Cumulative liquidity, this level and all better |

**`market_update`** - the market info diff (status, last traded price, volume, probability)
on a two second cadence.

**`request_snapshot`** - a client event. The server immediately pushes the current book and
market state. Use it after every reconnect instead of waiting:

```json
["0", "snap", "market:<market_id>", "request_snapshot", {}]
```

Markets in `pre_open`, `open`, `closed` or `cancelled` are joinable. Resulted and voided
markets send one final `market_update`, then the socket is dropped.

> **A quiet market publishes nothing.** Updates are emitted when the book changes, so on an
> inactive market the snapshot may be all you receive for minutes. Absence of updates is not
> a broken connection - confirm with heartbeats, not with book traffic.

### `markets` and `market_info` - the whole market list

`market_info` streams diffs for every market: a mandatory `market_id` and `timestamp`, plus
whichever fields changed. Status changes push immediately; the rest are batched.

`markets` carries `market_updated` and `market_created`. It supports **server-side
filtering** - pass rule filters and message types in the join payload and the server only
pushes what you asked for, rather than every market change.

Use these for discovery - noticing markets as they open - and `market:<id>` for depth on the
ones you care about.

## Private channels

All private topics are scoped by **user id**, and the server checks the topic against the
authenticated user: `active_orders:<user_id>`, `active_trades:<user_id>`,
`active_positions:<user_id>`, `active_settlements:<user_id>`, `portfolio:<user_id>`,
`user_info:<user_id>`. A mismatch returns `unauthorized` on join.

### `active_orders:<user_id>`

`all_orders` on join - every currently open order. `new_open_order` thereafter, carrying the
full order each time its state changes, including your `client_order_id`:

```
id=a6ea08a0...  status=open       action=buy  price=3600  quantity=1  filled=0  client_order_id=quickstart-...
id=a6ea08a0...  status=cancelled  action=buy  price=3600  quantity=1  filled=0  client_order_id=quickstart-...
```

Order prices here are **integer cents**, matching GraphQL - unlike the book channel. See
Units below.

### `active_trades:<user_id>`

`all_trades` on join, then `trade` per fill. This is your fill stream; do not poll
`myTradesHistory` for it.

### `active_positions:<user_id>`

`all_positions` on join, then `updated_positions` as positions move.

### `portfolio:<user_id>`

`summary` on join and `update` thereafter, carrying `available_balance` among other wallet
figures.

> Worth knowing: available balance is **not** reachable from GraphQL with an API key - the
> `account` query is not on the key allow-list - but it is delivered here. If your risk
> controls need balance, take it from this channel.

The server does not compute portfolio market value; combine positions with market prices
yourself.

### `active_settlements:<user_id>`

`new_settlements` - settlements as they are created, carrying a `settlements` list. This is
where realised P&L shows up: a position closing, or a market resolving. Take it from here
rather than polling `mySettlementsHistory`.

### `user_info:<user_id>`

`user_updated` - account status and profile changes.

## Cancel orders on disconnect

For an unattended quoting system this is the safety net: if your process dies, the exchange
pulls your quotes rather than leaving them resting.

Two halves, and both are required:

**1. Flag the orders.** Set `cancelOnDisconnect: true` in the `UserOrder` you pass to
`confirmOrder`. Orders without the flag are never auto-cancelled.

**2. Enable it on the channel**, by joining `active_orders` with a payload:

```json
["1", "1", "active_orders:<user_id>", "phx_join",
 {"cancel_on_disconnect": true, "ping_timeout": 10000}]
```

The reply confirms what the server actually accepted - it may clamp your timeout:

```json
{"status": "ok", "response": {"cancel_on_disconnect": true, "ping_timeout": 10000}}
```

Then send `ping` to that channel more often than `ping_timeout`:

```json
["1", "42", "active_orders:<user_id>", "ping", {}]
```

and it replies `{"ttl": 10000, "ping": "pong"}`. When pings stop for longer than the timeout,
the server treats the channel as dead and cancels every flagged order. Notes:

- An order flagged after the last ping is still cancelled when that timeout expires.
- Joining from several sockets is fine - cancellation only fires once **all** of them are
  considered dead.
- Closing the socket deliberately behaves the same way: cancellation follows the timeout, which
  gives you a window to reconnect.
- `ping` is per-channel and separate from the `phoenix`/`heartbeat` that keeps the socket
  itself open. You need both.
- Each ping also re-asserts the registration server-side, so the feature recovers on its own if
  a server process restarts underneath you. Another reason to keep pinging steadily rather than
  only when idle.

## Units

The two transports disagree, and it is the easiest way to be wrong by a factor of 100:

| | Price format | Example |
| --- | --- | --- |
| `order_book_update` | currency units, 2 dp | `49.0` |
| GraphQL, order and trade channels | integer cents | `4900` |

GraphQL rejects a non-integer price outright, so convert deliberately when a quote read from
the book becomes an order.

## Reconnecting

1. Reconnect the socket. If authenticated, **re-sign** - the old timestamp is long outside
   the ±30 second window.
2. Rejoin every topic, with fresh `join_ref`s.
3. Send `request_snapshot` on each market topic rather than waiting for the next publish.
4. Restart heartbeats, and the `ping` loop if you use cancel-on-disconnect.
5. Reconcile: `all_orders`, `all_trades` and `all_positions` arrive on join and are the
   authoritative state. Do not assume your in-memory view survived the gap.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| Connection closes after ~20s | No `phoenix`/`heartbeat` frames |
| `request_snapshot` or `ping` ignored | `join_ref` does not match the one used for `phx_join` |
| Handshake fails with HTTP 403 | Bad signature, clock skew, or missing the `X-` prefix |
| `unauthorized` joining a private channel | Topic user id is not the authenticated user |
| No book updates | The market is quiet - nothing has changed |
| Prices off by 100x | Book is dollars, orders are cents |

## Still stuck?

Ask in Discord - **https://discord.gg/yF9eVzPzNZ**. Include the operation or channel
name, the environment, and the exact error text. [SUPPORT.md](./SUPPORT.md) lists what
helps us answer in one round trip.
