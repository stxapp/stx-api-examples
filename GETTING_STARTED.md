# Getting started

> **Superseded.** This page predates the `/api/v1` REST API and describes the
> exchange as GraphQL-only, on hostnames we no longer publish. It is kept here
> only until [docs.stxapp.io](https://docs.stxapp.io) fully replaces it. Where it
> disagrees with [README.md](./README.md) or the docs site, it is wrong.

From clone to a live order in about ten minutes. Every step here has been run end to end
against Ontario staging.

## 1. Clone and install

Python 3.9 or newer.

```bash
git clone https://github.com/stxapp/stx-api-examples.git
cd stx-api-examples
python3 -m venv .venv
source .venv/bin/activate          # Windows: .\.venv\Scripts\Activate.ps1
pip install -r python/requirements.txt
cd python
```

Three dependencies: `cryptography`, `requests`, `websockets`. No STX SDK.

## 2. See market data before you have any credentials

Market and event discovery are public, so this works right now:

```bash
python stx_quickstart.py markets
```

```
[profile default -> staging.on.sportsxapp.com]

STXSOC-04JUN226FIFACUP-SPAIN                    46.0 / 52.0     max $100.00  World Cup Winner Spain
STXMLB-26AUG051410TORHOU-SPREADHOUMINUS1.5        -- / --       max $100.00  MLB TOR @ HOU -1.5
STXMLB-26AUG051410TORHOU-SPREADHOUMINUS2.5        -- / --       max $100.00  MLB TOR @ HOU -2.5
STXWNBA-26AUG082030SEAPOR-SPREADPORMINUS8.5       -- / --       max $100.00  WNBA SEA @ POR -8.5
```

And the live order book over WebSocket, also with no credentials:

```bash
python stx_quickstart.py book
```

```
--- update 1  (6 bids / 9 offers)
  bid      46.00 x       36   cum       36
  offer    52.00 x        5   cum        5
```

If both of those worked, your network path to the exchange is fine and everything from here is
just credentials.

## 3. Create an API key

1. Register and verify an account at https://staging.on.sportsxapp.com
2. **Account -> API Keys** -> **Create API Key**
3. Choose **read_write** if you want to place orders in step 5, otherwise **read_only**
4. Copy the **Key ID** and the **Private Key PEM**. The private key is shown once and we never
   store it, so save it before closing the dialog.

```bash
mkdir -p ~/.stx && chmod 700 ~/.stx
cat > ~/.stx/ontario-staging.pem      # paste the PEM, then Ctrl-D
chmod 600 ~/.stx/ontario-staging.pem
```

## 4. Save a profile

Credentials live in `~/.stx/credentials`, one section per environment. The STX Python SDK reads
the same file, so this carries over when you move to it.

```ini
[ontario-staging]
region      = ontario
env         = staging
key_id      = <your key id>
private_key = ~/.stx/ontario-staging.pem
user_id     = <ask us - see below>
```

```bash
chmod 600 ~/.stx/credentials
```

Then confirm signing works:

```bash
python stx_quickstart.py --profile ontario-staging orders
```

```
[profile ontario-staging -> staging.on.sportsxapp.com]

0 open order(s)
```

A count, even zero, means your signature verified. If you get `unauthorized`, it is almost
always clock skew (the timestamp must be within 30 seconds of ours) or a missing header - the
troubleshooting table that used to live in `python/README.md` has moved to
[docs.stxapp.io](https://docs.stxapp.io).

**About `user_id`:** the private WebSocket channels are scoped by user id, and there is
currently no self-service way to read yours, so ask us and we will send it. This is a gap we
are closing. You do not need it for anything in steps 2, 3, or 5 - only for the watcher in
step 6.

## 5. Place and cancel an order

Needs a `read_write` key. This places a real order on staging, priced well below the best bid
so it rests instead of filling, then cancels it:

```bash
python stx_quickstart.py --profile ontario-staging roundtrip
```

```
STXSOC-04JUN226FIFACUP-SPAIN  best bid $46.00 -> bidding $36.00
placed  a6ea08a0-96c2-4722-9afa-fe2bf94b33bd  status=accepted  filled=0
cancel  status=cancelled
```

That is the full loop: signed request, order accepted, order cancelled.

## 6. Watch it live

Open a second terminal. The watcher streams the book together with your own orders, fills,
positions, settlements and balance, on one authenticated socket:

```bash
# terminal 1
python stx_watch.py --profile ontario-staging --cancel-on-disconnect

# terminal 2
python stx_quickstart.py --profile ontario-staging roundtrip
```

Terminal 1 shows your order arrive and leave, and the public book depth change with it:

```
20:39:57  ORDER  id=a6ea08a0...  status=open       price=3600  client_order_id=quickstart-...
20:39:57  BOOK       36 @   46.00   |   52.00   @ 5        (7x9 levels)
20:39:57  ORDER  id=a6ea08a0...  status=cancelled  price=3600  client_order_id=quickstart-...
20:39:58  BOOK       36 @   46.00   |   52.00   @ 5        (6x9 levels)
```

This is the layout to develop against: watcher in one terminal, your strategy in another.

Three things worth noticing in that output:

- Your `client_order_id` comes back on every order event, so you can reconcile against your own
  book without storing our ids.
- The public book depth goes `6x9` -> `7x9` -> `6x9` as your order rests and is pulled. Your own
  order sits in the aggregated book anonymously, like everyone else's.
- Order prices on this channel are **cents** (`3600`), while book prices are dollars (`46.00`).
  Same session, two units.

## Four things that will save you time

**Prices use different units on the two transports.** The order book channel sends dollars
(`46.00`); GraphQL wants integer cents (`4600`), and rejects anything non-integer. Convert
deliberately - `stx_quickstart.py` has `cents()` and `dollars()` helpers.

**The socket needs heartbeats.** It closes after about 20 seconds of silence, and from the
client side that looks like updates simply stopping. Send `[null, "1", "phoenix", "heartbeat",
{}]` every 15 seconds. Both scripts do.

**Reuse the `join_ref`.** Phoenix frames are `[join_ref, ref, topic, event, payload]`, and a
message to a topic must carry the same `join_ref` you used to join it, or the server ignores it
silently. This is the usual reason `request_snapshot` appears to do nothing.

**A quiet market publishes nothing.** Book updates are emitted when the book changes, so
silence is not a broken connection. Use `request_snapshot` after any reconnect rather than
waiting for a tick.

## Where to go next

| | |
| --- | --- |
| [SOCKETS.md](./SOCKETS.md) | Every channel and event, cancel-on-disconnect, reconnect procedure |
| [Authentication](https://docs.stxapp.io/api/authentication/) | Signing in Python, Node.js, Go, Java, C# or shell, with a test vector |
| [README.md](./README.md) | The operation list, pagination, price units |
| [schema/](./schema) | GraphQL schema for editor autocomplete and code generation |
| [postman/](./postman) | One request per operation, foldered by what authentication it needs |

For a market-making loop specifically: quote with `confirmOrders`, tag every order with your
own `clientOrderId` and `cancelOnDisconnect: true`, re-quote with `cancelOrders` followed by
`confirmOrders` since there is no atomic replace, and take fills from the `active_trades`
channel rather than polling. Maker trades are commission free.

Stuck on any step above? Ask in Discord - **https://discord.gg/yF9eVzPzNZ** - where you will be talking to the
engineers who build the exchange. [Support](https://docs.stxapp.io/support/) lists what to include so we can
answer in one round trip, and we are happy to open a shared Slack channel for a live
integration.

See [Known issues](./README.md#known-issues) for the handful of rough edges we know about, so
none of them costs you an afternoon.
