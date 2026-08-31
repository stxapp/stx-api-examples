# STX API examples

Runnable examples for the [STX](https://stxapp.io) exchange API, organised by
language and then by surface: request signing, market data, order placement,
live order books over WebSocket, and a latency measurement you can run against
your own connection.

**This is example code, not a client library.** It is deliberately plain and
copyable: each script reads top to bottom and shows what goes on the wire. If
you want a maintained client instead, use **`pysdk`**, the supported STX Python
SDK; ask support for access.

```
install.sh  configure  verify       setup and signing check - POSIX shell only
python/     rest/  websockets/      signed REST, live channels, latency
javascript/ rest/  websockets/
postman/                            REST collection, one request per route
```

## Setup

```sh
git clone https://github.com/stxapp/stx-api-examples.git
cd stx-api-examples

./install.sh          # sets up only the runtimes you have; skips the rest
./configure           # stores your key id and private key under ~/.stx/
./verify              # signs GET /api/v1/me and prints your user_id and scope
```

`./verify` is the proof that setup worked: it signs a real request with nothing
but `curl` and `openssl`. If it prints a `user_id`, everything else here will
work.

[**GETTING_STARTED.md**](./GETTING_STARTED.md) walks through all of this one
step at a time, including creating the key, with the output you should expect at
each step.

## Examples

**REST**: read market data, then place an order and cancel it.
[Read market data](./GETTING_STARTED.md#4-read-market-data) ·
[Place and cancel an order](./GETTING_STARTED.md#5-place-and-cancel-an-order)

**WebSockets**: stream the book together with your own orders, trades,
positions and balance on one authenticated socket.
[Watch it live](./GETTING_STARTED.md#6-watch-it-live) ·
[Every channel](./CHANNELS.md)

**Latency**: measure the round trip from placing an order to seeing it on the
book, in both runtimes, on your own network path.
[Measure the round trip](./GETTING_STARTED.md#7-measure-the-round-trip)

Everything authenticates with an
[Ed25519 API key](./GETTING_STARTED.md#signing) and runs against the US
integration environment by default;
[`--profile`](./GETTING_STARTED.md#3-store-the-credentials-and-prove-they-sign)
selects another.

## Notes

- **Every `/api/v1` route requires your API key.** Reading market data needs a
  signed request just as much as placing an order does.
- **Prices use two representations.** Book prices are decimal-dollar strings
  (`"0.54"`); orders and `max_price` are integer cents (`54`, `100`).
- **The JavaScript REST examples have no dependencies.** Node has Ed25519 in
  `node:crypto` and `fetch` built in. Only the WebSocket examples use packages.
  Python needs `cryptography`, `requests` and `websockets`, which `./install.sh`
  puts in `python/.venv`.
- **`./configure` never takes key material as an argument** and never prints
  your private key.
- **The latency examples place real orders.** They rest below the touch so they
  do not fill, cancel what they place, and refuse a production profile.

Signing, price units, response shapes, socket timers and known issues are in
[Reference](./GETTING_STARTED.md#reference). The full API lives at
[docs.stxapp.io](https://docs.stxapp.io).

## Support

Questions are welcome in our Discord, where the answers are visible to everyone
building on the exchange and you are talking to the engineers who build it:

**https://discord.gg/yF9eVzPzNZ**

See [Support](https://docs.stxapp.io/support/) for what to include when asking
about a failing request, and for the shared-Slack option on larger integrations.

There are no enforced rate limits today. Prefer the batch cancel endpoints over
loops, and tell us your expected message rates so we watch the right things as
you scale.

## License

MIT. See [LICENSE](./LICENSE).
