# Request signing

The scheme in full, with a test vector so you can confirm your implementation before pointing
it at the exchange. Nothing here is Python-specific - any language with Ed25519 works, and the
snippets below are all verified to produce identical output.

## The scheme

Every authenticated request carries three headers:

| Header | Value |
| --- | --- |
| `STX-ACCESS-KEY` | your Key ID, from Account -> API Keys |
| `STX-ACCESS-TIMESTAMP` | current Unix time in **milliseconds**, as a decimal string |
| `STX-ACCESS-SIGNATURE` | base64 Ed25519 signature of the message below |

The message is three values concatenated with **no separator**:

```
message = timestamp_ms + HTTP_METHOD_UPPERCASE + request_path
```

For GraphQL, which always posts to the same path:

```
1700000000000POST/api/graphql
```

Rules that matter:

- **The request body is not signed.** Only timestamp, method and path.
- **The method is uppercase.** `POST`, not `post`.
- **The path includes its query string** when there is one, and never the scheme or host.
  `/api/v1/orders?status=open`, not `https://host/api/v1/orders?status=open`.
- **Pure Ed25519** (RFC 8032). Sign the UTF-8 bytes of the message directly. No pre-hashing,
  and not the `Ed25519ph` pre-hashed variant.
- **Standard base64** with padding. Not URL-safe base64.
- **±30 seconds.** The timestamp must be within 30 seconds of our clock, so generate it per
  request and keep your host on NTP. Do not cache or reuse a signature.

On the WebSocket the same three headers take an **`X-` prefix** - `X-STX-ACCESS-KEY` and so on -
because that transport only surfaces `x-*` headers. There you sign method `GET` against the
handshake path with the query string dropped:

```
1700000000000GET/socket/websocket
```

even though you connect to `/socket/websocket?vsn=2.0.0`. See [SOCKETS.md](./SOCKETS.md).

## Test vector

Ed25519 signatures are deterministic, so the same key and message always produce the same
signature. Check your implementation against this before doing anything else.

This key exists only for testing. It is not registered anywhere and will never authenticate a
real request - do not use it beyond verifying your code.

```
-----BEGIN PRIVATE KEY-----
MC4CAQAwBQYDK2VwBCIEIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4f
-----END PRIVATE KEY-----
```

| | |
| --- | --- |
| Message | `1700000000000POST/api/graphql` |
| Signature | `HDg79kZLqA4eJACLMafxLk4Pyi3iTjFP/u+iXqlkpCJb33ILkKAKshjyOsIHWkyNC+jbbjgpwdg4Sn1N2xgvBQ==` |

Byte-identical output means your key loading, message construction, signing mode and base64
encoding are all correct, and any later `unauthorized` is a timestamp, header or key-id problem
rather than a crypto one.

## Implementations

Each of these was run against the vector above and produced exactly that signature.

### Python

```python
import base64, time
from cryptography.hazmat.primitives import serialization

with open("test_key.pem", "rb") as fh:
    key = serialization.load_pem_private_key(fh.read(), password=None)

timestamp = str(int(time.time() * 1000))
message = f"{timestamp}POST/api/graphql".encode("utf-8")
signature = base64.b64encode(key.sign(message)).decode()
```

`pip install cryptography`. Full example: [python/stx_quickstart.py](./python/stx_quickstart.py).

### Node.js

```javascript
import { createPrivateKey, sign } from 'crypto';
import { readFileSync } from 'fs';

const key = createPrivateKey(readFileSync('test_key.pem'));
const timestamp = Date.now().toString();
const message = Buffer.from(`${timestamp}POST/api/graphql`, 'utf8');
const signature = sign(null, message, key).toString('base64');
```

No dependencies - Ed25519 is in the standard `crypto` module. Passing `null` as the first
argument to `sign` selects pure Ed25519.

### Go

```go
import (
    "crypto/ed25519"
    "crypto/x509"
    "encoding/base64"
    "encoding/pem"
    "fmt"
    "os"
    "time"
)

data, _ := os.ReadFile("test_key.pem")
block, _ := pem.Decode(data)
parsed, _ := x509.ParsePKCS8PrivateKey(block.Bytes)
key := parsed.(ed25519.PrivateKey)

timestamp := fmt.Sprintf("%d", time.Now().UnixMilli())
message := []byte(timestamp + "POST/api/graphql")
signature := base64.StdEncoding.EncodeToString(ed25519.Sign(key, message))
```

Standard library only.

### Java

```java
import java.nio.file.*;
import java.security.*;
import java.security.spec.PKCS8EncodedKeySpec;
import java.util.Base64;

String pem = Files.readString(Path.of("test_key.pem"))
    .replaceAll("-----[A-Z ]+-----", "").replaceAll("\\s", "");
PrivateKey key = KeyFactory.getInstance("Ed25519")
    .generatePrivate(new PKCS8EncodedKeySpec(Base64.getDecoder().decode(pem)));

String timestamp = String.valueOf(System.currentTimeMillis());
Signature signer = Signature.getInstance("Ed25519");
signer.initSign(key);
signer.update((timestamp + "POST/api/graphql").getBytes("UTF-8"));
String signature = Base64.getEncoder().encodeToString(signer.sign());
```

Java 15 or newer, no dependencies.

### Shell

Useful for a one-off curl or for filling in Postman variables:

```bash
TS=$(python3 -c 'import time; print(int(time.time()*1000))')
SIG=$(printf "%sPOST/api/graphql" "$TS" \
  | openssl pkeyutl -sign -inkey ~/.stx/ontario-staging.pem -rawin \
  | openssl base64 -A)

curl -s https://staging.on.sportsxapp.com/api/graphql \
  -H "Content-Type: application/json" \
  -H "STX-ACCESS-KEY: $STX_KEY_ID" \
  -H "STX-ACCESS-TIMESTAMP: $TS" \
  -H "STX-ACCESS-SIGNATURE: $SIG" \
  -d '{"query":"query { myOrderHistory { totalCount } }"}'
```

`-rawin` is what selects pure Ed25519. OpenSSL 1.1.1 or newer.

### C#

.NET has no built-in Ed25519 as of .NET 8, so use a library - [NSec](https://nsec.rocks/) or
BouncyCastle - or use the [STX C# SDK](https://www.nuget.org/packages/STX.Sdk), which handles
signing for you.

## Generating your own key

Either let us generate the pair when you create the key, or bring your own and register the
public half:

```bash
openssl genpkey -algorithm ed25519 -out ~/.stx/ontario-staging.pem
openssl pkey -in ~/.stx/ontario-staging.pem -pubout
```

The second command prints the SPKI PEM public key to paste when creating the API key. The
private key never leaves your machine, and we cannot recover it.

## When a signature will not verify

Work through these in order. The server deliberately returns the same `unauthorized` for every
signature failure, so the cause has to come from your side.

1. Is the method uppercase in the message?
2. Does the path match exactly, including any query string, and exclude scheme and host?
3. On the WebSocket: did you sign `GET` and `/socket/websocket` **without** `?vsn=2.0.0`, and use
   the `X-` prefixed header names?
4. Is the base64 standard and padded, rather than URL-safe?
5. Are you signing the message bytes rather than a hash of them?
6. Is the timestamp in the message byte-for-byte the one in the header?
7. Is your clock within 30 seconds of ours? `curl -sI https://staging.on.sportsxapp.com | grep -i date`

If the test vector above reproduces exactly and a real request still fails, the problem is the
Key ID, the key's status, or the clock - not the signing.

## Still stuck?

Ask in Discord - **https://discord.gg/yF9eVzPzNZ**. Include the operation or channel
name, the environment, and the exact error text. [SUPPORT.md](./SUPPORT.md) lists what
helps us answer in one round trip.
