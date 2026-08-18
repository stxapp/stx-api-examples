# STX API examples

Working examples for integrating with the [STX](https://stxapp.io) exchange directly - API key
signing, market data over WebSocket, and order placement. No SDK dependency, so these stay
valid regardless of which client library you use, or none at all.

If you want a client library instead, see the STX SDKs for Python and C#. This repository is
the layer underneath: what the requests and frames actually look like.

### Start here: [GETTING_STARTED.md](./GETTING_STARTED.md)

Clone to a live order in about ten minutes.

```
GETTING_STARTED.md  clone, install, create a key, place an order
SIGNING.md          request signing in any language, with a test vector
SUPPORT.md          where to ask questions, and what to include
SOCKETS.md          WebSocket reference - channels, events, reconnect, cancel-on-disconnect
python/             the two example scripts
schema/             GraphQL schema for the operations an integration needs
postman/            Postman collection, one request per operation
```

The rest of this file is reference material: signing, pagination, price units, and the full
operation list.

## How the API is shaped

Two transports, and you will use both:

- **GraphQL over HTTP** at `/api/graphql` - queries and mutations. Place and cancel orders,
  read your orders, trades and settlements, discover markets.
- **WebSocket** at `/socket/websocket` - everything real-time. Order book depth, your fills,
  order state changes, positions, balance. See [SOCKETS.md](./SOCKETS.md).

There are no REST trading endpoints today.

Access comes in three tiers:

| Tier | Needs | Covers |
| --- | --- | --- |
| Public | nothing | Market and event discovery, order book channels |
| API key | signed headers | Placing and cancelling orders; reading your own orders, trades, settlements, positions |
| Session | interactive login | Everything else - profile, payments, 2FA, limits |

## Create an API key

1. Register and verify an account. On staging: https://staging.on.sportsxapp.com
2. Go to **Account → API Keys**
3. **Create API Key**, label it, and choose a scope:
   - `read_only` - reads only
   - `read_write` - also places and cancels orders
4. Copy the **Key ID** and the **Private Key PEM**. The private key is shown once and is never
   stored by the server. Lose it and you revoke the key and issue a new one.

Keys are Ed25519. You can also generate your own pair and register only the public half:

```bash
openssl genpkey -algorithm ed25519 -out ~/.stx/ontario-staging.pem
openssl pkey -in ~/.stx/ontario-staging.pem -pubout    # paste this when creating the key
```

## Credentials

Credentials live in `~/.stx/credentials`, an INI file with one section per environment. This
is the same file and profile mechanism the STX Python SDK reads, so what you set up here
carries over.

```ini
[default]
region      = ontario
env         = staging
key_id      = bf98340d96993755d8a4974e31d2991a
private_key = ~/.stx/ontario-staging.pem
user_id     = 1f7c9a34-...        ; only needed for private WebSocket channels

[ontario-production]
region      = ontario
env         = production
key_id      = 7c21ab...
private_key = ~/.stx/ontario-production.pem
```

```bash
chmod 700 ~/.stx && chmod 600 ~/.stx/credentials ~/.stx/*.pem
```

On Windows the path is `%USERPROFILE%\.stx\credentials`; see [`python/README.md`](./python/README.md)
for locking it down with `icacls`.

Select a profile with `--profile <name>` or `STX_PROFILE`. Any value can be overridden by an
environment variable - `STX_KEY_ID`, `STX_PRIVATE_KEY`, `STX_REGION`, `STX_ENV`, `STX_HOST`,
`STX_USER_ID` - which take precedence over the file.

A key belongs to exactly one environment, so keep one section and one PEM per environment and
name them so they cannot be confused.

### Hosts

The host is derived from `region` + `env`, so you never hand-type one:

| region | env | host |
| --- | --- | --- |
| ontario | staging | staging.on.sportsxapp.com |
| ontario | production | api.on.stxapp.ca |

**Ontario staging is where to build.** You can register and self-verify there, and it runs the
same code as production.

US endpoints are not published yet and will be added to this table when they are. In the
meantime, set `host` in the profile, or `STX_HOST`, to point anywhere else.

## Signing a request

Three headers on every authenticated call:

| Header | Value |
| --- | --- |
| `STX-ACCESS-KEY` | your Key ID |
| `STX-ACCESS-TIMESTAMP` | Unix time in **milliseconds**, as a string |
| `STX-ACCESS-SIGNATURE` | base64 Ed25519 signature of the message below |

The message is a bare concatenation - no separators, and the request body is not part of it:

```
timestamp_ms + HTTP_METHOD_UPPERCASE + request_path
```

Every GraphQL call therefore signs `<timestamp>POST/api/graphql`. Sign the UTF-8 bytes with
pure Ed25519 (RFC 8032) - no pre-hashing, not the `Ed25519ph` variant - and encode with
standard padded base64, not URL-safe. The timestamp must be within **±30 seconds** of the
server clock, so generate it per request and keep your machine on NTP.

On the WebSocket the same three headers take an **`X-` prefix** and you sign method `GET`
against the handshake path without its query string: `<timestamp>GET/socket/websocket`. See
[SOCKETS.md](./SOCKETS.md).

**Not using Python?** [SIGNING.md](./SIGNING.md) has verified implementations in Node.js, Go,
Java and shell, plus a test vector - a fixed key, message and expected signature - so you can
confirm your code produces the right bytes before sending a request.

## Prices

The two transports use different units, which is the easiest way to be wrong by a factor of
100:

| | Format | Example |
| --- | --- | --- |
| `order_book_update` on the socket | currency units, 2 dp | `49.0` |
| GraphQL, and order/trade channels | integer cents | `4900` |

GraphQL rejects a non-integer price outright, so convert deliberately when a quote read from
the book becomes an order.

## Pagination

Two styles, depending on the query.

**Cursor (keyset)** - `marketInfosWithCount` only, and the one to use when walking the market
list, which runs to tens of thousands of rows. Send `limit` for the first page, then pass the
returned `cursor` back for the next. `cursor` comes back `null` when there is nothing more:

```graphql
query Markets($input: MarketInfosInput) {
  marketInfosWithCount(input: $input) {
    count            # total matching the filters, before pagination
    cursor           # feed this back as pagination.cursor
    marketInfos { marketId symbol title }
  }
}
```

```json
{ "input": { "pagination": { "limit": 500 } } }
{ "input": { "pagination": { "limit": 500, "cursor": "g2gDbQAAABAb_n1EdLFQ2S4y..." } } }
```

**Offset** - everything else: `myOrderHistory`, `myTradesHistory`, `mySettlementsHistory`,
`accountMarketStats`, `marketSettlements`, `myDepositAndWithdrawalHistory`. Pass
`pagination: { limit, page }`, where **`page` starts at 1**.

## Operations

The [schema](./schema/stx-schema-integration.graphql) and the
[Postman collection](./postman/stx-api.postman_collection.json) cover the same set.

**Public - no authentication**

`marketInfos`, `marketInfosWithCount`, `eventInfos`, `eventsMarketsInfo`, `marketFilterTreeV2`,
`termsAndConditions`

**API key - reads**

`accountMarketStats`, `myOrderHistory`, `myTradesHistory`, `myTradesForOrder`,
`mySettlementsHistory`, `marketSettlements`, `tradesForSettlement`,
`myDepositAndWithdrawalHistory`

**API key - orders (`read_write`)**

`confirmOrder`, `confirmOrders`, `cancelOrder`, `cancelOrders`, `cancelAllOrders`, `tncAccepted`

Prefer the batch forms over loops when managing many orders - `cancelOrders` takes up to 1,000
ids per call.

Operations outside this set either need an interactive session or are not part of trading:
loyalty, casino, payments, authentication and 2FA, profile, devices and limits, and
`placeOddsOrder` / `placeRiskOrder`. The live endpoint exposes them; introspect it for the full
picture.

## Other resources

| | |
| --- | --- |
| Full API documentation | https://wiki.stxapp.io/en/trading-api |
| Interactive schema explorer | `/graphiql` on any environment |
| C# SDK | [NuGet](https://www.nuget.org/packages/STX.Sdk) · [docs](https://wiki.stxapp.io/en/stx-csharp-sdk) |
| JavaScript examples | https://github.com/stxapp/js-demo |
| Python SDK | In beta - ask support for access |

REST endpoints are not available; GraphQL and WebSockets are the supported path.

## Known issues

Current as of this writing, and worth knowing before you hit them:

- **`myTradesForOrder` returns HTTP 500 for an order id that does not exist or is not yours**,
  rather than an empty list. Pass ids you got from `myOrderHistory`. Fix in progress.
- **`pagination.page` must be 1 or greater.** The schema describes `page` as accepting 0, but
  sending 0 returns a 500. Start at 1.
- **There is no self-service way to read your own user id**, which the private WebSocket
  channels need for their topics. Ask us and we will send it. We are adding it to the API.

## Support

Questions are welcome in our Discord, where answers are visible to everyone building on the
exchange, and you are talking to the engineers who build it:

**https://discord.gg/yF9eVzPzNZ**

See [SUPPORT.md](./SUPPORT.md) for what to include when asking about a failing request, and for
the shared-Slack option on larger integrations.

Rate limits: there are none enforced today. Prefer the batch operations over loops, and tell us
your expected message rates so we can keep an eye on the right things as you scale.

## License

MIT - see [LICENSE](./LICENSE).
