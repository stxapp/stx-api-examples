// Builds the collection the Live workflow runs with newman, from the published
// postman/stx-rest-api.postman_collection.json, and leaves that file untouched.
//
//   node .github/scripts/postman-ci.mjs <out.json>
//
// The published collection is meant to be clicked through by hand: it has no
// tests, and `Get an order` and `Cancel an order` read an order id you paste in.
// This keeps the collection's signing script and requests, and adds what an
// unattended run needs:
//
//   * a test on every request: HTTP 200 and a JSON body
//   * `List markets` picks a market a 1 @ $0.01 buy will rest on
//   * `Place an order` buys 1 @ $0.01 there, and saves the order id
//   * the order is read, cancelled, and checked to be gone from the open list
//
// Requests that change anything beyond that one order are skipped. Every
// request in the collection must be named in RUN or SKIP below, so one added
// later fails this script until someone decides which it belongs in.

import { readFileSync, writeFileSync } from "node:fs";

const SOURCE = new URL("../../postman/stx-rest-api.postman_collection.json", import.meta.url);

// Run in this order. A name may appear twice to send the same request again.
const RUN = [
  "Get the authenticated account",
  "List markets",
  "List events",
  "Place an order",
  "List orders",
  "Get an order",
  "Cancel an order",
  "List orders",
  "List open positions",
  "Get account balance",
  "List fills",
  "Get account market stats",
  "List settlements",
  "List deposits",
  "List withdrawals",
  "List fees",
  "List adjustments",
  "Get loyalty status",
];

const SKIP = {
  "Update your public profile": "changes the account's public profile",
  "Place several orders": "places more than the one order",
  "Cancel several orders": "covered by Cancel an order",
  "Cancel all open orders": "would cancel orders this run did not place",
  "Accept terms and conditions": "changes the account's consent record",
};

// Every request gets this.
const BASE_TEST = `
pm.test("HTTP 200", function () { pm.response.to.have.status(200); });
pm.test("JSON body", function () { pm.response.to.be.json; });
`;

// Extra assertions, by request name and occurrence (0 for the first).
const EXTRA_TESTS = {
  "List markets#0": `
const markets = pm.response.json().markets || [];
// A buy at $0.01 fills only against an offer at $0.01, so skip any market
// that has one. Everything else leaves the order resting.
const restsAtOneCent = (m) => !(m.offers || []).some((o) => Number(o.price) <= 0.01);
const market = markets.find((m) => m.trading && m.status === "open" && restsAtOneCent(m));
pm.test("a tradeable market to rest a $0.01 bid on", function () {
  pm.expect(market, "no open, trading market without a $0.01 offer").to.be.ok;
});
if (market) {
  pm.environment.set("market_id", market.market_id);
  console.log("market", market.symbol);
}
`,
  "Place an order#0": `
const order = pm.response.json().order || {};
pm.test("order accepted, nothing filled", function () {
  pm.expect(order.id, "order id").to.be.a("string");
  pm.expect(order.price).to.eql("0.0100");
  pm.expect(Number(order.filled)).to.eql(0);
});
if (order.id) {
  pm.environment.set("order_id", order.id);
  console.log("placed", order.id, order.status);
}
`,
  "List orders#0": `
const ids = (pm.response.json().orders || []).map((o) => o.id);
pm.test("the order is open", function () {
  pm.expect(ids).to.include(pm.environment.get("order_id"));
});
`,
  "Get an order#0": `
const body = pm.response.json();
const order = body.order || body;
pm.test("reads back the order placed", function () {
  pm.expect(order.id).to.eql(pm.environment.get("order_id"));
  pm.expect(order.status).to.not.eql("cancelled");
});
`,
  "Cancel an order#0": `
const body = pm.response.json();
const order = body.order || body;
pm.test("order cancelled", function () {
  pm.expect(order.status).to.eql("cancelled");
});
`,
  "List orders#1": `
const ids = (pm.response.json().orders || []).map((o) => o.id);
pm.test("the order is no longer open", function () {
  pm.expect(ids).to.not.include(pm.environment.get("order_id"));
});
`,
};

// The order: the smallest size at the lowest price, tagged as this run's.
const ORDER_BODY = {
  market_id: "{{market_id}}",
  order_type: "limit",
  action: "buy",
  price: "0.01",
  quantity: "1",
  client_order_id: "ci-postman-{{$timestamp}}",
};

const collection = JSON.parse(readFileSync(SOURCE, "utf8"));
const byName = new Map();
for (const folder of collection.item) {
  for (const item of folder.item ?? [folder]) {
    if (byName.has(item.name)) throw new Error(`duplicate request name: ${item.name}`);
    byName.set(item.name, item);
  }
}

const unclassified = [...byName.keys()].filter((n) => !RUN.includes(n) && !(n in SKIP));
const missing = [...new Set([...RUN, ...Object.keys(SKIP)])].filter((n) => !byName.has(n));
if (unclassified.length || missing.length) {
  if (unclassified.length) console.error(`Not in RUN or SKIP: ${unclassified.join(", ")}`);
  if (missing.length) console.error(`Named here but not in the collection: ${missing.join(", ")}`);
  process.exit(1);
}

const seen = {};
const items = RUN.map((name) => {
  const occurrence = (seen[name] = (seen[name] ?? -1) + 1);
  const item = structuredClone(byName.get(name));
  if (occurrence > 0) item.name = `${name} (${occurrence + 1})`;
  if (name === "Place an order") {
    item.request.body.raw = JSON.stringify(ORDER_BODY, null, 2);
  }
  const script = BASE_TEST + (EXTRA_TESTS[`${name}#${occurrence}`] ?? "");
  item.event = [{ listen: "test", script: { type: "text/javascript", exec: script.split("\n") } }];
  return item;
});

const out = process.argv[2];
if (!out) {
  console.error("usage: node .github/scripts/postman-ci.mjs <out.json>");
  process.exit(2);
}
writeFileSync(out, JSON.stringify({ ...collection, item: items }, null, 2));
console.log(`${items.length} requests, ${Object.keys(SKIP).length} skipped: ${Object.keys(SKIP).join("; ")}`);
