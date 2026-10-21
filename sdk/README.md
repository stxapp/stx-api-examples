# SDK examples

The crypto example from [GETTING_STARTED.md](../GETTING_STARTED.md#4b-trade-the-crypto-windows), written with the STX SDKs instead of raw signed requests. Same commands, same output. The SDK signs each request, follows cursors and turns error responses into exceptions.

Both SDKs read the `~/.stx/credentials` file that `./configure` writes, and use its `[default]` profile. To use another one, set `STX_PROFILE`, for example `export STX_PROFILE=ontario-demo` after `./configure ontario-demo`. [Authentication](https://docs.stxapp.io/sdks/python/authentication/) covers environment variables and the other ways to pass a key.

## Python

[`stx-python`](https://pypi.org/project/stx-python/), Python 3.9 or later.

Give the SDK its own virtualenv, not `python/.venv`: the SDK's `stx` package would hide the `python/stx.py` helper the plain examples import.

```bash
python3 -m venv sdk/python/.venv && source sdk/python/.venv/bin/activate
pip install -r sdk/python/requirements.txt

python sdk/python/crypto.py windows
python sdk/python/crypto.py book --coin btc --side up
python sdk/python/crypto.py quote --coin btc --side up --bid 0.45 --offer 0.55
python sdk/python/crypto.py results --coin eth
```

Guide: [docs.stxapp.io/sdks/python](https://docs.stxapp.io/sdks/python/)

## TypeScript

[`@stxapp/stx-typescript`](https://www.npmjs.com/package/@stxapp/stx-typescript), Node 20 or later. The example runs with `tsx`, so there is no build step.

```bash
cd sdk/typescript
npm ci

npx tsx crypto.ts windows
npx tsx crypto.ts book --coin btc --side up
npx tsx crypto.ts quote --coin btc --side up --bid 0.45 --offer 0.55
npx tsx crypto.ts results --coin eth
```

Guide: [docs.stxapp.io/sdks/typescript](https://docs.stxapp.io/sdks/typescript/)

## Sample output

```text
$ python sdk/python/crypto.py quote --coin btc --side up --bid 0.45 --offer 0.55
STXCRYPTO-26OCT210215-UPBTCMIN15  closes 00:30:00 MDT (7m05s left)
book       best bid 0.4800  best offer 0.5200
placed     BUY  1 @ 0.45  7ba5c8ac-f205-4703-9d4d-f755a60bab7a  status=accepted
placed     SELL 1 @ 0.55  f0d7855a-08df-43df-9b32-085a56a3524a  status=accepted
on book    best bid 0.4800  best offer 0.5200
cancelled  7ba5c8ac-f205-4703-9d4d-f755a60bab7a  status=cancelled
cancelled  f0d7855a-08df-43df-9b32-085a56a3524a  status=cancelled
```

`quote` needs a `read_write` key and only places orders on a demo exchange unless you pass `--force-production`.
