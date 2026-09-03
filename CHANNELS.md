# WebSocket channels

Every channel the STX socket exposes, with the frame to join it and the payload
it sends. The legacy payloads are captured from `demo.stxapp.io`; the
dollar-format ones are built from the server's serializers, which that host does
not yet run.

There are two families. The **dollar-format topics** send money as a decimal
string in dollars, matching `/api/v1` field for field, and are what every example
in this repository joins. The **legacy topics** send integer cents; they still
work and are not deprecated, but nothing here uses them and new code should not
start there. Each legacy topic names its replacement.

One authenticated socket carries all of them. Sign the handshake and join
whatever you need on that one connection: `python/websockets/watch.py` joins
seven, and the rest are here because they exist and are joinable, not because an
example uses them.

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
`[join_ref, ref, topic, "phx_join", payload]`, so `--topic orders:...` puts
`["0", "0", "orders:...", "phx_join", {}]` on the wire, character for character,
and prints whatever comes back unformatted.

`--topic` is repeatable and `<user_id>` is substituted for you:

```sh
python python/websockets/watch_channel.py --topic ticker --topic 'balances:<user_id>'
node javascript/websockets/watch_channel.mjs --topic market_updates
```

`--payload` sets the join payload, which the public topics use for filtering:

```sh
python python/websockets/watch_channel.py --topic orderbook \
  --payload '{"market_ids": ["<market_id>"]}'
```

Use `watch.py` instead when you want the whole picture: it joins `orderbook`,
`ticker` and your five user channels at once and formats what it recognises. Its
own `--topic` adds to that set rather than replacing it.

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
  --topic 'user_info:2b7f41ac-95d0-4e18-b3c6-8a1f0d572e34'
```

It prints both directions: the join frame it sends, marked `->`, then every
frame that comes back, marked `<-` and unformatted. What this page documents is
literally what goes on the wire.

```
16:39:55  ticker             -> ["0","0","ticker","phx_join",{}]
16:39:55  balances           -> ["1","1","balances:<user_id>","phx_join",{}]

16:39:55  ticker             <- phx_reply  {"status":"ok","response":{ ... }}
16:39:55  balances           <- phx_reply  {"status":"ok","response":{}}
16:39:55  balances           <- balances  {"available_balance":"10000.73", ... }
```

## Two timers, not one

There are two, and it is the single easiest thing to get wrong by hand:

| Timer | Reset by | Window | Miss it and |
| --- | --- | --- | --- |
| Socket keep-alive | a heartbeat on the `phoenix` topic | 60s | the connection closes |
| `cancel_on_disconnect` | a `ping` on the `orders:` topic | 5000-20000 ms, whatever you negotiated at join | **your flagged orders are cancelled** on a connection that is still up |

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

## Dollar-format topics

Money is a decimal string in dollars, quantities are strings, and counts stay
plain integers - the same wire format as `/api/v1`. See
[Prices](./GETTING_STARTED.md#prices).

Five of these replace a legacy topic one for one: same events, same join rules,
same payload keys, different number format. `balances:` is the exception, and
`account:` has no legacy twin.

| Legacy topic | Dollar topic | Differences beyond the format |
| --- | --- | --- |
| `market:<market_id>` | `orderbook` + `ticker` | one topic for all markets, no join snapshot; the book and the market summary are split |
| `active_orders:<user_id>` | `orders:<user_id>` | none |
| `active_trades:<user_id>` | `fills:<user_id>` | none |
| `active_positions:<user_id>` | `positions:<user_id>` | none |
| `active_settlements:<user_id>` | `settlements:<user_id>` | none |
| `portfolio:<user_id>` | `balances:<user_id>` | different join event and field list |
| - | `account:<user_id>` | the five private topics on one join |
| - | `ticker` | public per-market price summary |
| - | `trades` | public market-wide executions |

**Do not join a topic and its legacy twin on the same socket.** You will receive
every update twice, once in each format.

`user_info:` has no dollar topic because it carries no monetary values.

The public payloads below are captured from `demo.stxapp.io`, like the legacy
ones. The private payloads are shortened to the fields worth showing.

### `orderbook` - aggregated book, public

One topic covering every market, narrowed by the join payload. Ten markets is
one join, not ten. **At least one valid `market_id` is required**: unlike every
other filter on this socket, an absent or unusable list is an error rather than
"no filter", because an unfiltered firehose of every book is not something this
serves.

```json
["0", "0", "orderbook", "phx_join", {"market_ids": ["a7f9bdfb-7702-44bd-b4d9-6eee282f6041"]}]
```

```sh
python python/websockets/watch_channel.py --topic orderbook \
  --payload '{"market_ids": ["<market_id>"]}'
```

The reply echoes the markets actually applied, so a mistyped id shows up there
rather than as silence:

```json
{"status": "ok", "response": {"selected_market_ids": ["a7f9bdfb-7702-44bd-b4d9-6eee282f6041"]}}
```

No book arrives on join. `GET /api/v1/markets` is the opening state; this keeps
it current. Then `book`, one message per market, on the book proc's cadence:

```json
{"market_id": "51e1bf3e-e61c-4591-bcc4-5f5efba892c1",
 "bids": [{"quantity": "517.00", "price": "0.5800", "liquidity": "299.8600",
           "total_quantity": "517.00", "total_liquidity": "299.8600"}],
 "offers": [{"quantity": "810.00", "price": "0.6300", "liquidity": "510.3000",
             "total_quantity": "810.00", "total_liquidity": "510.3000"}],
 "timestamp": "2026-09-03T20:55:36.662254Z", "timestamp_us": 1788468936662254}
```

Only the first level of each side is shown; a real frame carries the whole book.

Levels are in fill order, best first. `liquidity` is `quantity x price`;
`total_quantity` and `total_liquidity` are cumulative through that level.

**Every push is a complete snapshot of that market's book, not a delta.** Replace
whatever you hold for that `market_id` wholesale rather than merging into it.

`select_market_ids` changes the selection without rejoining:

```json
["0", "1", "orderbook", "select_market_ids", {"market_ids": ["<other market_id>"]}]
```

### `ticker` - per-market price summary, public

```json
["1", "1", "ticker", "phx_join", {}]
["1", "1", "ticker", "phx_join", {"sports": ["Baseball"], "competitions": ["MLB"]}]
```

```sh
python python/websockets/watch_channel.py --topic ticker
```

Pushes `ticker` for each market whose price, book top, volume or open interest
moved:

```json
{"market_id": "e2e7ecf3-8100-4723-a9b7-9f8c276bff61",
 "market_symbol": "STXNFL-26SEP131625WSHPHI-RECYDSPHISBARKLEY409916-15.5",
 "event_id": "b2dd8ccd-c70a-4873-b245-5d306904bbad",
 "event_symbol": "STXNFL-26SEP131625WSHPHI",
 "sport": "Football", "competition": "NFL",
 "last_traded_price": null, "last_traded_quantity": null,
 "best_bid": null, "best_bid_quantity": null,
 "best_offer": null, "best_offer_quantity": null,
 "bid_depth": 0, "offer_depth": 0,
 "open_interest": "0.00", "total_volume": "0.00",
 "timestamp": "2026-09-03T20:55:33.942863Z", "timestamp_us": 1788468933942863}
```

That is a real frame from a market with no book and no trades yet, which is why
so much of it is `null` - worth seeing, because it is the common case on a
market that has just opened. `sport` is `"Football"`, capitalised as the market
carries it: filter values are matched exactly, so `"football"` would match
nothing.

`bid_depth` and `offer_depth` count price levels, so they are integers rather
than quantity strings. Any field can be `null` on a market that has not traded or
has an empty side of the book, and `event_symbol` is `null` while the event list
is still warming.

Sides are named `offer`, not `ask`, matching the REST market payload.

**No snapshot on join.** This is a change feed: nothing arrives until a market
moves. Fetch `GET /api/v1/markets` for the initial state.

`ticker` is close to the legacy `markets` channel in shape - one global topic, no
id in the topic string, narrowed by the join payload - but it answers a different
question. `markets` is a **discovery** feed: `market_created` and
`market_updated`, keyed by market id, carrying a diff of whichever fields
changed. `ticker` is a **price summary**: a fixed field set, complete every push,
only when the price, book top, volume or open interest moved. Nothing in the
dollar family reports a market being created, so `markets` remains the only way
to notice one appearing without polling.

Note also that `ticker` has no `market_ids` filter. Watching one market means
narrowing by sport or competition and then dropping the rest on `market_id`
yourself - `watch.py` and `watch.mjs` both do exactly that, next to the
server-side filter `orderbook` gets, so the two are easy to compare.

### `trades` - executed trades, public and anonymous

```json
["2", "2", "trades", "phx_join", {}]
["2", "2", "trades", "phx_join", {"market_ids": ["<uuid>"], "event_ids": ["<uuid>"]}]
```

```sh
python python/websockets/watch_channel.py --topic trades
```

`trade`, one message per execution:

```json
{"market_id": "a7f9bdfb-7702-44bd-b4d9-6eee282f6041",
 "market_symbol": "STXMLB-26AUG311805SFATL-GAMEATL",
 "event_id": "1f7d2e61-14ca-45f8-8966-0c25bbc75b96",
 "event_symbol": "STXMLB-26AUG311805SFATL",
 "price": "0.6700", "quantity": "3.00", "action": "buy",
 "timestamp": "2026-09-03T14:31:11.402910Z", "timestamp_us": 1788208271402910}
```

`action` is the **taker's** side: `buy` when the incoming order bought from the
book, `sell` when it sold into it. No account, user or order identifier is
carried.

**`trades` is not `fills:<user_id>`.** This is every trader's executions; `fills:`
is yours. They differ by one letter and are not interchangeable.

### Filters on the public topics

Every filter follows one contract:

- omitted, `null`, `[]`, or a list with no usable entry means **no filter**
  (except `orderbook`'s `market_ids`, which is required);
- unusable entries are dropped rather than rejecting the join;
- the join reply echoes what was actually applied, so compare it against what
  you sent to catch a typo;
- naming two filters **narrows** - a message must match both;
- `select_filters`, or `select_market_ids` on `orderbook`, changes them without
  rejoining.

### `orders:<user_id>` - your orders

Scoped by user, not by market, so it fires for every order you have regardless of
which market you are watching. A user id that is not yours fails the join with
`unauthorized`.

```json
["3", "3", "orders:<user_id>", "phx_join", {}]
["3", "3", "orders:<user_id>", "phx_join", {"market_ids": ["<uuid>"]}]
```

```sh
python python/websockets/watch_channel.py --topic 'orders:<user_id>'
```

`all_orders` on join, then `new_open_order` per change - the same event names
`active_orders:` uses, so migrating needs no re-tagging:

```json
{"id": "324e4890-e7c2-4e6f-bff4-5059ab3daf34", "status": "cancelled",
 "action": "buy", "price": "0.5100", "quantity": "1.00", "filled": "0.00",
 "client_order_id": "quickstart-1788208870",
 "cancellation_reason": "by_player", "rejection_reason": null}
```

This is also the topic that takes `cancel_on_disconnect`; see
[Two timers, not one](#two-timers-not-one).

### `fills:<user_id>` - your executions

```json
["4", "4", "fills:<user_id>", "phx_join", {}]
```

```sh
python python/websockets/watch_channel.py --topic 'fills:<user_id>'
```

`all_trades` on join, then `trade`. `total_fee` is the all-in fee, trade fee plus
settlement fee, so a REST snapshot and a delta from here can be mixed;
`trade_fee` is sent separately for the on-trade component.

### `positions:<user_id>` - your positions

```json
["5", "5", "positions:<user_id>", "phx_join", {}]
```

```sh
python python/websockets/watch_channel.py --topic 'positions:<user_id>'
```

`all_positions` on join, then `updated_positions`. The event is
`updated_positions`, not `new_positions` - an exact-match binding on the wrong
name drops it in silence.

### `settlements:<user_id>` - realised profit and loss

```json
["6", "6", "settlements:<user_id>", "phx_join", {}]
```

```sh
python python/websockets/watch_channel.py --topic 'settlements:<user_id>'
```

`new_settlements` as they are created. **There is no join snapshot** - use
`GET /api/v1/portfolio/settlements` for history.

### `balances:<user_id>` - balances

The one dollar topic that is not a drop-in for its legacy twin. It differs from
`portfolio:` in three ways: **the join event is `balances`, not `summary`**; it
carries no gaming fields; and it takes an optional `account_id`.

```json
["7", "7", "balances:<user_id>", "phx_join", {}]
["7", "7", "balances:<user_id>", "phx_join", {"account_id": "<account_id>"}]
```

```sh
python python/websockets/watch_channel.py --topic 'balances:<user_id>'
```

A user may hold more than one account and `portfolio:` can only ever serve the
first; name one here to reach another. An account that is not yours is rejected
as `unauthorized`, which never reveals whether it exists. Join twice, once per
account, and each socket receives only its own account's frames.

`balances` on join, then `update` as it changes and `payment_update` on payments:

```json
{"account_id": "...", "user_id": "...",
 "available_balance": "10000.73", "account_balance": "10000.73",
 "buy_order_liability": "0.0000", "sell_order_liability": "0.0000",
 "position_premium_liability": "0.0000",
 "total_deposits": "10000.00", "total_withdrawals": "0.0000",
 "total_settlement_pnl": "0.7300", "total_fees": "0.0000",
 "total_adjustments": "0.0000", "escrow": "0.0000",
 "total_trade_count": 4, "base_fee_percent": 0.02,
 "fee_schedule": "on_trade", "taker_factor": 0.02, "maker_factor": 0.0}
```

`total_trade_count` is a count and the three factor/percent fields are rates, so
those stay numbers. Spendable balance is rounded **down** and liabilities **up**,
at cent precision, so neither is ever overstated in your favour.

The server does not compute portfolio market value; combine positions with market
prices yourself.

### `account:<user_id>` - all five on one join

No legacy twin. Carries everything the five private topics above carry, with the
same events and payloads - a subscription convenience, not a different feed.

```json
["8", "8", "account:<user_id>", "phx_join", {}]
["8", "8", "account:<user_id>", "phx_join", {"market_ids": ["<uuid>"]}]
```

```sh
python python/websockets/watch_channel.py --topic 'account:<user_id>'
```

On join you receive `all_orders`, `all_trades`, `all_positions` and `balances`.
After that `new_open_order`, `trade`, `updated_positions`, `new_settlements`,
`update` and `payment_update` arrive as they happen. There is no settlements
snapshot, because `settlements:` does not send one either.

**Do not also join a per-type topic.** A socket joined to both `account:` and,
say, `orders:` receives every order twice. Pick one.

**`account:` does not support `cancel_on_disconnect`.** Its `ping` keeps the
session alive but arms no cancellation. If you rely on resting orders being
pulled when your socket drops, use the per-type topics - for everything, not
alongside `account:`. This is why `watch.py` joins the five rather than the one.

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

## Legacy cents topics

Everything below still works and is not deprecated, but it sends integer cents
and no example here joins it. Each section names its replacement above. New code
should start with the dollar topics.

### `market:<market_id>` - one market's book

Superseded by [`orderbook`](#orderbook---aggregated-book-public).

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
through this level. Note `ob` is in dollars and `bids` is in cents.

**These are not REST's units.** The topics in this file predate the dollar-string
format and were not converted: they still send cents, while every money and
quantity field on `/api/v1` is now a decimal string in dollars. The same market
reports `max_price` as `100` in the payload above and as `"1.0000"` over REST.
[Prices](./GETTING_STARTED.md#prices) puts the two side by side. Do not carry a
number from one into the other.

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

### `markets` - markets appearing and changing

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
change:

```json
["1", "1", "markets", "phx_join",
 {"rule_filters": ["home_winner"], "message_types": ["market_updated"]}]
```

```sh
python python/websockets/watch_channel.py --topic markets \
  --payload '{"rule_filters": ["home_winner"], "message_types": ["market_updated"]}'
```

which the server confirms by echoing back what it accepted:

```json
{"selected_rule_filters": ["home_winner"],
 "selected_message_types": ["market_updated"]}
```

**The keys you send are not the keys you get back.** Send `rule_filters` and
`message_types`; the reply reports them as `selected_rule_filters` and
`selected_message_types`. Sending the `selected_` names is accepted and then
ignored.

Read that echo, because nothing here fails loudly. A misspelled rule, an event
name that does not exist, or a bare string where an array belongs are all
dropped in silence, and the reply comes back with `selected_rule_filters: null`,
meaning no filter at all. You asked to narrow the stream and quietly got every
market instead. The defaults are `null` rules and both message types.

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

### `active_orders:<user_id>` - your orders

Superseded by [`orders:<user_id>`](#ordersuser_id---your-orders).

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

`price` is integer cents here, unlike the book and unlike the same order read
back over REST, where it is `"0.5100"`. `client_order_id` is echoed back
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

### `active_trades:<user_id>` - your fills

Superseded by [`fills:<user_id>`](#fillsuser_id---your-executions).

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

### `active_positions:<user_id>` - your positions

Superseded by [`positions:<user_id>`](#positionsuser_id---your-positions).

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

### `active_settlements:<user_id>` - realised profit and loss

Superseded by [`settlements:<user_id>`](#settlementsuser_id---realised-profit-and-loss).

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

### `portfolio:<user_id>` - balance

Superseded by [`balances:<user_id>`](#balancesuser_id---balances).

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

## Reconnecting

A dropped socket loses state silently, so treat a reconnect as a cold start:

1. Re-sign the handshake. The old timestamp is outside the 30-second window.
2. Rejoin every topic, with fresh `join_ref`s.
3. Send `request_snapshot` on each `market:` topic.
4. Restart both timers, the `phoenix` heartbeat and the `active_orders` `ping`.
5. Reconcile from `all_orders`, `all_trades` and `all_positions`. They are the
   authoritative state; do not assume your in-memory view survived the gap.
