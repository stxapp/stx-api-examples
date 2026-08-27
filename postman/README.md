# Postman collection

`stx-rest-api.postman_collection.json` covers every `/api/v1` route: identity,
markets, events, orders, trades, positions and portfolio history. Import it and
set `base_url` and `key_id` in the collection variables.

## Postman cannot sign for you

Every `/api/v1` route requires an Ed25519 signature — there are no public REST
endpoints — and **Postman's script sandbox has no Ed25519 implementation**. There
is no pre-request script that can produce a valid signature without embedding a
hand-written Ed25519 into the collection, which is not something to put in an
example you are meant to trust.

So two of the collection variables are filled in from outside, per request:

| Variable | Value |
| --- | --- |
| `timestamp` | Unix **milliseconds**, as a string |
| `signature` | base64 Ed25519 signature of `timestamp + METHOD + path` |

The signed message is a bare concatenation with no separators, the path includes
the query string exactly as you send it, and **the body is not signed**. The
timestamp must be within 30 seconds of the server clock, so a signature is good
for one request and about half a minute.

## Generating a pair

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
