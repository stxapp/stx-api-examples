# SDK examples

The crypto example from [GETTING_STARTED.md](../GETTING_STARTED.md#4b-trade-the-crypto-windows), written with the STX SDKs instead of raw signed requests. Same commands, same output. The SDK signs each request, follows cursors and turns error responses into exceptions.

Both SDKs read the `~/.stx/credentials` file that `./configure` writes. Pick a profile with `STX_PROFILE`, or leave it unset to use `[default]`. [Authentication](https://docs.stxapp.io/sdks/python/authentication/) covers environment variables and the other ways to pass a key.

## Python

[`stx-python`](https://pypi.org/project/stx-python/), Python 3.9 or later.

Give the SDK its own virtualenv, not `python/.venv`: the SDK's `stx` package would hide the `python/stx.py` helper the plain examples import.

```bash
python3 -m venv sdk/python/.venv && source sdk/python/.venv/bin/activate
pip install -r sdk/python/requirements.txt

export STX_PROFILE=us-demo
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

export STX_PROFILE=us-demo
npx tsx crypto.ts windows
npx tsx crypto.ts book --coin btc --side up
npx tsx crypto.ts quote --coin btc --side up --bid 0.45 --offer 0.55
npx tsx crypto.ts results --coin eth
```

Guide: [docs.stxapp.io/sdks/typescript](https://docs.stxapp.io/sdks/typescript/)

## Sample output

```text
$ python sdk/python/crypto.py quote --coin btc --side up --bid 0.45 --offer 0.55
STXCRYPTO-26OCT162345-UPBTCMIN15  closes 22:00:00 MDT (11m31s left)
book       best bid -  best offer -
placed     BUY  1 @ 0.45  e694cd0b-8aec-4607-bb96-17c91edaa379  status=accepted
placed     SELL 1 @ 0.55  51678edd-af71-4587-8067-fb1cbdec3f63  status=accepted
on book    best bid 0.4500  best offer 0.5500
cancelled  e694cd0b-8aec-4607-bb96-17c91edaa379  status=cancelled
cancelled  51678edd-af71-4587-8067-fb1cbdec3f63  status=cancelled
```

`quote` needs a `read_write` key and only places orders on a demo exchange unless you pass `--force-production`.
