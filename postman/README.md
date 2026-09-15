# Postman collection

`stx-rest-api.postman_collection.json` covers every `/api/v1` route: identity,
markets, events, orders, fills, positions and portfolio history.

## Setup

1. Import the collection.
2. Set `key_id` in the collection variables. `base_url` already points at
   `https://demo.stxapp.io`.
3. Create a Postman **environment** and put your Ed25519 private key in it as
   `stx_private_key` - either the PKCS#8 PEM shown once when the key was
   created, or the raw 32-byte seed as base64. Both are accepted.

Then send any request. The collection signs for you.

**Keep the key in an environment, not in the collection.** Collection variables
travel inside the exported JSON, so a key there can end up in a repository or a
shared workspace. The collection deliberately does not declare
`stx_private_key` for that reason.

Two requests need an id you copy from an earlier response: `Get an order` and
`Cancel an order` read `order_id`, and `Place an order` reads `market_id` from
the body. Run `List markets` and `Place an order` first, or set them by hand.

## How the signing works

The collection's pre-request script inlines
[tweetnacl-js](https://github.com/dchest/tweetnacl-js) v1.0.3, which is public
domain, because Postman's sandbox has no Ed25519 primitive and will not let a
script `require` one - `crypto`, `node:crypto`, `tweetnacl` and
`@noble/ed25519` are all blocked. It signs locally and sets the three
`X-STX-ACCESS-*` headers on the way out; no key material leaves your machine.

The signed message is `timestamp + METHOD + path`, a bare concatenation with no
separators. The path **includes the query string** exactly as sent, and the
**body is not signed**. The timestamp must be within 30 seconds of the server
clock.

Two things in that script are load-bearing and easy to break:

- `require` is shadowed to `void 0` before tweetnacl loads. tweetnacl probes for
  a Node PRNG with `require('crypto')`, which *throws* in the sandbox rather
  than returning undefined, and the throw would take the whole script down.
  Ed25519 signing needs no randomness, so nothing is lost.
- The path is resolved with `pm.variables.replaceIn(...)`. At pre-request time
  `getPathWithQuery()` still returns the raw `{{order_id}}` template, so signing
  it verbatim while the request goes to the resolved id is a 401.

Because it runs in the sandbox, `newman run` signs too, so the collection works
in CI and on Windows without a shell.

## If a request comes back 401

The server returns the same `Missing or invalid API key credentials` for every
authentication failure, so the status alone will not tell you which one. In
order of likelihood:

1. `stx_private_key` is not set in the active environment, or the wrong
   environment is selected.
2. `key_id` does not match that private key.
3. Your clock is more than 30 seconds off. Check NTP.
4. The key lacks the scope for the route - placing or cancelling needs
   `read_write`.

## Signing by hand instead

If you would rather not put a private key into Postman at all, leave
`stx_private_key` unset and fill the `timestamp` and `signature` collection
variables yourself before each request. The script throws a clear error when the
key is absent, and these two variables still feed the headers.

Paste this into a shell once, then call `stx_sign` before each request:

```sh
stx_sign() {                       # stx_sign GET /api/v1/me
    ts="$(date +%s)000"
    msg=$(mktemp)
    printf '%s' "$ts$1$2" > "$msg"
    sig=$(openssl pkeyutl -sign -inkey ~/.stx/default.pem -rawin -in "$msg" \
        | openssl base64 -A)
    rm -f "$msg"
    echo "timestamp = $ts"
    echo "signature = $sig"
}
```

The message has to reach openssl as a **file**, via `-in`. Ed25519 is one-shot,
so openssl wants the length up front: piping to stdin fails with `unable to
determine file size for oneshot operation`, and so does a plain `<` redirect.

Copy the two values into the collection variables of the same name.

The `openssl` on macOS is LibreSSL, which cannot sign with Ed25519. Install
OpenSSL 3 (`brew install openssl@3`) and put it first on `PATH`.

## Or skip Postman

The signing is the part Postman makes awkward, and everything else here does it
for you:

```sh
./verify                                  # curl + openssl, GET /api/v1/me
node javascript/rest/quickstart.mjs me    # zero dependencies
python python/rest/quickstart.py me
```
