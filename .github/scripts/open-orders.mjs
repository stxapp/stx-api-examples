// Checks that the orders a Live run placed are no longer open, and cancels any
// that are, so a failed run does not leave orders resting on the demo book.
//
//   node .github/scripts/open-orders.mjs <order_id>...
//
// Exits 1 if any of the given orders was still open. Other open orders on the
// account are reported as a warning only: other clients may share the key.
// Uses the same profile resolution and signing as the examples.

import { loadProfile, signedHeaders } from "../../javascript/stx.mjs";

const placed = new Set(process.argv.slice(2).filter(Boolean));
const config = loadProfile();

async function call(method, path) {
  const response = await fetch(config.baseUrl + path, {
    method,
    headers: signedHeaders(config, method, path),
  });
  const text = await response.text();
  if (!response.ok) throw new Error(`${method} ${path}: HTTP ${response.status} ${text.slice(0, 300)}`);
  return JSON.parse(text);
}

const open = [];
let path = "/api/v1/orders?status=open&limit=100";
for (;;) {
  const page = await call("GET", path);
  open.push(...page.orders);
  if (!page.cursor) break;
  path = `/api/v1/orders?status=open&limit=100&cursor=${encodeURIComponent(page.cursor)}`;
}

const leftOver = open.filter((o) => placed.has(o.id));
const others = open.filter((o) => !placed.has(o.id));

console.log(`${placed.size} order(s) placed by this run; ${open.length} open on the account`);
for (const order of leftOver) {
  const cancelled = await call("DELETE", `/api/v1/orders/${order.id}`);
  console.log(`::error::order ${order.id} (${order.client_order_id}) was still open; cancelled it, status=${cancelled.status}`);
}
if (others.length) {
  console.log(`::warning::${others.length} other open order(s) on the account, not placed by this run`);
}
process.exit(leftOver.length ? 1 : 0);
