# WebSocket channels

Every channel the STX socket exposes, with the frame to join it and a real
payload captured from `demo.stxapp.io`.

One authenticated socket carries all of them. Sign the handshake and join
whatever you need on that one connection: `python/websockets/watch.py` joins six
of the nine below, and the rest are here because they exist and are joinable,
not because an example uses them.

Sign for every channel. The user-scoped channels reject an unsigned join with
`{"reason": "unauthorized"}`, and the market channels are expected to require a
signature in future, so there is nothing to gain by treating them differently.

Frames are `[join_ref, ref, topic, event, payload]`. The handshake, profile
setup and the walkthrough that gets you to a live order are in
[GETTING_STARTED.md](./GETTING_STARTED.md).

## Trying a channel

Each channel below shows two blocks. The first is the `phx_join` frame itself,
which is what your own client puts on the socket: send that array and you are
joined. The second is a `watch_channel.py` command that sends exactly that frame
for you, so you can see the channel before writing any code.

They really are the same thing. `watch_channel.py` builds every join as
`[join_ref, ref, topic, "phx_join", payload]`, so `--topic active_orders:...`
puts `["0", "0", "active_orders:...", "phx_join", {}]` on the wire, character
for character, and prints whatever comes back unformatted.

`--topic` is repeatable and `<user_id>` is substituted for you:

```sh
python python/websockets/watch_channel.py --topic markets --topic 'portfolio:<user_id>'
node javascript/websockets/watch_channel.mjs --topic market_updates
```

Use `watch.py` instead when you want the whole picture: it joins the market book
and your five user channels at once and formats what it recognises. Its own
`--topic` adds to that set rather than replacing it.

`<user_id>` is substituted for you, so the commands below run as written. If you
want the real value, `./verify` prints it:

```
OK
  user_id   2b7f41ac-95d0-4e18-b3c6-8a1f0d572e34
  scope     read_write
```

which makes the expanded form of the same command:

```sh
python python/websockets/watch.py \
  --topic 'active_orders:2b7f41ac-95d0-4e18-b3c6-8a1f0d572e34'
```

Frames arrive exactly as the server sends them, so a channel with no special
handling anywhere still shows its events:

```
joining markets
joining portfolio:<user_id>

16:25:05  markets            <- phx_reply  {"status":"ok","response":{ ... }}
16:25:05  portfolio          <- phx_reply  {"status":"ok","response":{}}
16:25:05  portfolio          <- summary  {"available_balance":1000073, ... }
```

## Two timers, not one

There are two, and it is the single easiest thing to get wrong by hand:

| Timer | Reset by | Window | Miss it and |
| --- | --- | --- | --- |
| Socket keep-alive | a heartbeat on the `phoenix` topic | 60s | the connection closes |
| `cancel_on_disconnect` | a `ping` on the `active_orders` topic | 5000-20000 ms, whatever you negotiated at join | **your flagged orders are cancelled** on a connection that is still up |

A 30-second heartbeat keeps the socket alive and still blows the `ping`
deadline. Send the channel `ping` on its own timer, not in response to traffic:
a quiet market produces no traffic and the deadline does not care.

The heartbeat is not on any channel. It goes to the `phoenix` topic with a null
`join_ref`:

```json
[null, "1", "phoenix", "heartbeat", {}]
```

**Reuse the `join_ref`.** A message to a topic must carry the same `join_ref`
you used to join it, or the server ignores it silently. This is the usual reason
`request_snapshot` appears to do nothing.

**A quiet market publishes nothing.** Book updates are emitted when the book
changes, so silence is not a broken connection. Use `request_snapshot` after any
reconnect rather than waiting for a tick.

## `market:<market_id>` - one market's book

One market's order book and market-info stream.

```json
["0", "0", "market:a7f9bdfb-7702-44bd-b4d9-6eee282f6041", "phx_join", {}]
```

```sh
python python/websockets/watch_channel.py --topic 'market:<market_id or symbol>'
```

The join reply is not an acknowledgement: it carries the whole market, 51 keys
including the opening book. This is why `latency.py` settles on the join reply
rather than waiting for a push, and why a quiet market still gives you a book
immediately.

```json
{"ob": {"b": [{"p": 0.61, "q": 491.0, "l": 299.51, "tc": 491.0, "tl": 299.51}],
        "o": [{"p": 0.66, "q": 882.0, "l": 582.12, "tc": 882.0, "tl": 582.12}]},
 "bids": [{"quantity": 491, "price": 61}],
 "last_traded_price": null,
 "status": "open", "trading": true, "max_price": 100}
```

Book levels are in fill order, best first. `p` price, `q` contracts, `l`
liquidity (`q x p`), `tc` and `tl` the cumulative contracts and liquidity
through this level. Note `ob` is in dollars and `bids` is in cents; see
[Prices](./GETTING_STARTED.md#prices).

Then two ongoing events:

- **`order_book_update`** carries the same `ob` shape, on the server's cadence
  of roughly 200 ms, and only while the book is changing.
- **`market_update`** carries a market-info diff, roughly every two seconds:
  status, last traded price, volume, probability.

Client to server, on this topic only:

```json
["0", "snap", "market:<market_id>", "request_snapshot", {}]
```

The server immediately pushes the current book and market state. Use it after
every reconnect instead of waiting for a tick. Neither example sends it, since
both take the book from the join reply.

Markets in `pre_open`, `open`, `closed` and `cancelled` are joinable. A resulted
or voided market sends one final `market_update` and then the server drops the
socket, which arrives as `phx_close`. That is a terminal status, not a network
fault.

## `markets` - markets appearing and changing

No id in the topic. This is the discovery channel: it tells you when markets
are created or updated, so you notice a market opening without polling.

```json
["1", "1", "markets", "phx_join", {}]
```

```sh
python python/websockets/watch_channel.py --topic 'markets'
```

The join reply describes the server-side filtering it supports:

```json
{"selected_message_types": ["market_updated", "market_created"],
 "selected_rule_filters": null,
 "available_rules": ["ad_hoc_rule", "away_winner", "home_winner",
                     "event_stat_line", "combo_rule", "..."]}
```

`available_rules` lists around sixty market rules. Pass filters in the join
payload and the server pushes only what you asked for, rather than every market
change. The exact filter keys are not published; the reply above is the whole of
what the API tells you about them.

Events are `market_updated` and `market_created`. The payload is keyed by market
id rather than being a flat object:

```json
{"6a6052f9-0d32-417c-a63e-98d68c10e514": { ... changed fields ... }}
```

A diff carries a `market_id` and `timestamp` plus whichever fields changed.
Status changes push immediately, the rest are batched.

`market_updates` is a separate joinable topic on the same theme. Note the
plural: joining `market_update` singular fails with
`{"reason": "unmatched topic"}`, which is easy to hit because `market_update`
**is** a valid event name on `market:<market_id>`. Same string, two meanings.

## `active_orders:<user_id>` - your orders

Scoped by user, not by market, so it fires for every order you have regardless
of which market you are watching. A user id that is not yours fails the join
with `unauthorized`.

```json
["4", "4", "active_orders:<user_id>", "phx_join", {}]
```

```sh
python python/websockets/watch_channel.py --topic 'active_orders:<user_id>'
```

`all_orders` arrives once on join and is the authoritative state:

```json
{"orders": []}
```

Then `new_open_order` on every state change, carrying the whole order each time,
not just new ones:

```json
{"id": "324e4890-e7c2-4e6f-bff4-5059ab3daf34", "status": "cancelled",
 "action": "buy", "price": 51, "quantity": 1, "filled": 0,
 "client_order_id": "quickstart-1788208870",
 "cancellation_reason": "by_player", "rejection_reason": null}
```

`price` is integer cents here, unlike the book. `client_order_id` is echoed back
on every event, so you can reconcile against your own records without storing
exchange ids.

This is also the only channel that takes join options:

```json
["4", "4", "active_orders:<user_id>", "phx_join",
 {"cancel_on_disconnect": true, "ping_timeout": 10000}]
```

The reply echoes what the server actually accepted, which may be clamped, so use
the echoed value rather than the one you asked for:

```json
{"cancel_on_disconnect": true, "ping_timeout": 10000}
```

`ping_timeout` is clamped to 5000-20000 ms; a non-integer fails the join with
`{"ping_timeout": "Must be an integer"}`. Keep it alive with a `ping` on this
topic, on a timer at about half the negotiated timeout:

```json
["4", "42", "active_orders:<user_id>", "ping", {}]
```

which replies `{"ping": "pong", "ttl": 10000}`.

Three things about `cancel_on_disconnect` that surprise people. Orders must also
carry the flag themselves; unflagged orders are never auto-cancelled. If several
sockets have joined, cancellation fires only once all of them are gone. And a
deliberate close behaves like a drop, which gives you a reconnect window rather
than an immediate cancel.

## `active_trades:<user_id>` - your fills

Your fill stream. Take fills from here rather than polling.

```json
["5", "5", "active_trades:<user_id>", "phx_join", {}]
```

```sh
python python/websockets/watch_channel.py --topic 'active_trades:<user_id>'
```

```json
{"trades": []}
```

`all_trades` on join, then one event per fill. The examples bind `new_trade`;
older documentation called it `trade`. Neither name was observed during writing,
because confirming it needs a fill rather than a resting order, so treat the
ongoing event name as the one thing on this page that has not been verified
against the server.

## `active_positions:<user_id>` - your positions

Your open positions.

```json
["6", "6", "active_positions:<user_id>", "phx_join", {}]
```

```sh
python python/websockets/watch_channel.py --topic 'active_positions:<user_id>'
```

`all_positions` on join, then **`updated_positions`** as they change:

```json
{"positions": [{"id": "a7f9bdfb-7702-44bd-b4d9-6eee282f6041",
                "position": 0,
                "market_id": "a7f9bdfb-7702-44bd-b4d9-6eee282f6041",
                "event_id": "1f7d2e61-14ca-45f8-8966-0c25bbc75b96",
                "buy_order_liability": 0}]}
```

A position row arrives with `position` at 0 while you have only resting orders;
`buy_order_liability` is what those orders have committed.

## `active_settlements:<user_id>` - realised profit and loss

Where realised P&L shows up, when a position closes or a market resolves. Take it from here rather than polling.

```json
["7", "7", "active_settlements:<user_id>", "phx_join", {}]
```

```sh
python python/websockets/watch_channel.py --topic 'active_settlements:<user_id>'
```

`all_settlements` on join and `new_settlements` as they are created, both
carrying a `settlements` list. An account with no settlements receives no
snapshot at all on join, so absence is not an error.

## `portfolio:<user_id>` - balance

Available balance is delivered here. If your risk controls need balance, take
it from this channel.

```json
["8", "8", "portfolio:<user_id>", "phx_join", {}]
```

```sh
python python/websockets/watch_channel.py --topic 'portfolio:<user_id>'
```

`summary` on join, then **`update`** as it changes:

```json
{"available_balance": 1000073, "account_balance": 1000073,
 "buy_order_liability": 0, "sell_order_liability": 0,
 "fee_schedule": "on_trade", "loyalty_tier": "rookie"}
```

Balances are integer cents. The server does not compute portfolio market value;
combine positions with market prices yourself.

## `user_info:<user_id>` - account and profile

Emits `user_updated` on account status and profile changes. The payload
is your account profile: name, address, date of birth, country, account flags
such as `test_account`. Nothing about trading, and real personal data, so it is
listed here for completeness rather than shown.

```json
["9", "9", "user_info:<user_id>", "phx_join", {}]
```

```sh
python python/websockets/watch_channel.py --topic 'user_info:<user_id>'
```

## Reconnecting

A dropped socket loses state silently, so treat a reconnect as a cold start:

1. Re-sign the handshake. The old timestamp is outside the 30-second window.
2. Rejoin every topic, with fresh `join_ref`s.
3. Send `request_snapshot` on each `market:` topic.
4. Restart both timers, the `phoenix` heartbeat and the `active_orders` `ping`.
5. Reconcile from `all_orders`, `all_trades` and `all_positions`. They are the
   authoritative state; do not assume your in-memory view survived the gap.
