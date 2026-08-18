# Support

## Ask in Discord

**https://discord.gg/yF9eVzPzNZ**

That is the fastest way to reach us, and answers there are visible to everyone else building on
the exchange, so your question helps the next person too. Integration questions, unexpected
responses, missing operations, feature requests - all welcome.

You will be talking to the engineers who build the exchange, not a support tier.

## Before you ask about a failing request

A few details turn "it does not work" into an answer in one round trip:

- The **operation name** (`confirmOrder`, `myOrderHistory`) or the **channel topic**
  (`market:<id>`, `active_orders:<id>`)
- The **environment** you are on, and whether you are using an API key or a session token
- The **exact error text**, and the HTTP status if there was one
- For signing failures: whether the [test vector](./SIGNING.md#test-vector) reproduces on your
  machine. If it does, the problem is the timestamp, the key id, or the key's status rather than
  your crypto - and knowing that immediately narrows it down

Never paste your private key or its contents. We never need it, and we cannot help you faster by
seeing it. The Key ID alone is fine.

## Things that are already answered

| Symptom | Where |
| --- | --- |
| `unauthorized` on a signed request | [SIGNING.md](./SIGNING.md#when-a-signature-will-not-verify) |
| Signature will not verify at all | [test vector](./SIGNING.md#test-vector) |
| Socket goes quiet after ~20 seconds | [SOCKETS.md](./SOCKETS.md#keep-the-connection-alive) |
| `request_snapshot` or `ping` seems ignored | [SOCKETS.md](./SOCKETS.md#frame-format) - reuse the `join_ref` |
| Prices out by a factor of 100 | [README.md](./README.md#prices) - dollars on the book, cents in GraphQL |
| `Operation not supported for <scope> key` | [README.md](./README.md#operations) |
| Known rough edges | [README.md](./README.md#known-issues) |

## A shared Slack channel

For an integration of any size we are happy to open a shared Slack channel with our engineers,
which tends to work better than a ticket queue for live development. Ask in Discord or through
support and we will set it up.

## Reporting something security-sensitive

If you believe you have found a vulnerability, do not open it in Discord or a public issue.
Contact us privately through support and we will route it to the right people.
