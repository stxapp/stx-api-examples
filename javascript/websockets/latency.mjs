// Measure the place-order to book-update round trip.
//
//   node javascript/websockets/latency.mjs
//   node javascript/websockets/latency.mjs --rounds 10 --market <market_id>
//
// Each round places a resting limit order over REST, waits for the order book
// push that reflects it on the socket, then cancels it and waits again. It
// reports three numbers per leg:
//
//   REST   the HTTP round trip - request out, response in
//   WS     from that response to the socket push that shows the change
//   TOTAL  the two together, which is what your quoting loop actually sees
//
// python/websockets/latency.py is the same measurement in Python, so the two
// can be compared directly: same exchange, same market, different runtime and
// different WebSocket library.
//
// This places REAL orders. It refuses to run against a production profile
// unless --force-production is passed.
//
// Needs `./install.sh` (or `npm install` in javascript/) for phoenix and ws.

import { Socket } from "phoenix";
import WebSocket from "ws";
import { loadProfile, signedHeaders, parseArgs, fail, SOCKET_PATH } from "../stx.mjs";

const BOOK_TIMEOUT_MS = 15_000;

const args = parseArgs();
const config = loadProfile(args.profile);
const rounds = Number(args.rounds ?? 5);

if (config.environment === "production" && !args["force-production"]) {
  fail(
    `Refusing to place orders against production.\n` +
      `Profile [${config.profile}] points at ${config.baseUrl}.\n` +
      `Run this against an integration profile, or pass --force-production if you\n` +
      `really mean to put real orders on a real book.`
  );
}

console.error(`[${config.profile} -> ${config.baseUrl}]`);

// ---------------------------------------------------------------------------
// REST
// ---------------------------------------------------------------------------

async function rest(method, path, body) {
  const response = await fetch(config.baseUrl + path, {
    method,
    headers: {
      ...signedHeaders(config, method, path),
      "Content-Type": "application/json",
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    fail(`${method} ${path} -> HTTP ${response.status}: ${(await response.text()).slice(0, 300)}`);
  }
  return response.json();
}

const { markets } = await rest("GET", "/api/v1/markets?status=open&limit=200");

let market;
if (args.market) {
  market = markets.find((m) => m.market_id === args.market || m.symbol === args.market);
  if (!market) fail(`Market ${args.market} is not open and tradeable right now.`);
} else {
  const live = markets.filter((m) => m.trading && m.status === "open" && m.bids?.length > 0);
  if (live.length === 0) fail("No tradeable market with a bid to price against.");
  market = live.reduce((best, m) =>
    m.bids.length + (m.offers?.length ?? 0) > best.bids.length + (best.offers?.length ?? 0)
      ? m
      : best
  );
}

const bestBid = market.bids[0].price;

// Well under the touch so it rests rather than fills. A fill would measure the
// matching engine instead of the book publish, and would leave a position
// behind.
const price = Math.max(1, bestBid - 10);

// ---------------------------------------------------------------------------
// Socket
// ---------------------------------------------------------------------------

class SignedTransport extends WebSocket {
  // Re-signed per transport, so reconnects get a fresh timestamp inside the
  // 30-second window.
  constructor(url) {
    super(url, { headers: signedHeaders(config, "GET", SOCKET_PATH) });
  }
}

const socket = new Socket(config.socketEndpoint, {
  transport: SignedTransport,
  heartbeatIntervalMs: 20_000,
});
socket.connect();

const channel = socket.channel(`market:${market.market_id}`, {});

await new Promise((resolve, reject) => {
  channel
    .join()
    .receive("ok", resolve)
    .receive("error", (reason) => reject(new Error(JSON.stringify(reason))));
}).catch((error) => fail(`could not join the market channel: ${error.message}`));

/**
 * Resolve on the next order_book_update push.
 *
 * The book publishes on the server's own cadence - roughly every 200 ms - and
 * coalesces changes in between. So the WS figure below is dominated by where in
 * that window the order landed, not by network time. It is the number that
 * matters for a quoting loop even so: it is how long until you can see your own
 * order on the book you are quoting from.
 */
function nextBookUpdate() {
  return new Promise((resolve, reject) => {
    const ref = channel.on("order_book_update", () => {
      clearTimeout(timer);
      channel.off("order_book_update", ref);
      resolve();
    });
    const timer = setTimeout(() => {
      channel.off("order_book_update", ref);
      reject(new Error(`no order_book_update within ${BOOK_TIMEOUT_MS}ms`));
    }, BOOK_TIMEOUT_MS);
  });
}

// ---------------------------------------------------------------------------
// Rounds
// ---------------------------------------------------------------------------

console.log(`${market.symbol}  best bid ${bestBid}c, quoting ${price}c, ${rounds} round(s)`);
console.log(
  `${"ROUND".padEnd(7)} ${"LEG".padEnd(8)} ${"REST".padStart(9)} ${"WS".padStart(9)} ${"TOTAL".padStart(9)}`
);

await nextBookUpdate().catch(() => {}); // settle: let the first push go by

const samples = [];
let order = null;

try {
  for (let round = 1; round <= rounds; round++) {
    for (const leg of ["place", "cancel"]) {
      const started = performance.now();
      const pushSeen = nextBookUpdate();

      if (leg === "place") {
        ({ order } = await rest("POST", "/api/v1/orders", {
          market_id: market.market_id,
          order_type: "limit",
          action: "buy",
          price,
          quantity: 1,
          client_order_id: `latency-${Date.now()}`,
        }));
      } else {
        await rest("DELETE", `/api/v1/orders/${order.id}`);
      }

      const responded = performance.now();
      await pushSeen;
      const seen = performance.now();

      const restMs = responded - started;
      const wsMs = seen - responded;
      const totalMs = seen - started;
      samples.push({ leg, restMs, wsMs, totalMs });

      console.log(
        `${String(round).padEnd(7)} ${leg.padEnd(8)} ` +
          `${restMs.toFixed(1).padStart(7)}ms ${wsMs.toFixed(1).padStart(7)}ms ${totalMs.toFixed(1).padStart(7)}ms`
      );
    }
  }
} catch (error) {
  console.error(`\nstopped early: ${error.message}`);
}

// ---------------------------------------------------------------------------
// Summary
// ---------------------------------------------------------------------------

if (samples.length > 0) {
  const mean = (pick) => samples.reduce((sum, s) => sum + pick(s), 0) / samples.length;
  const totals = samples.map((s) => s.totalMs).sort((a, b) => a - b);
  const p50 = totals[Math.floor(totals.length / 2)];

  console.log(`\n${samples.length} samples`);
  console.log(`  REST  mean ${mean((s) => s.restMs).toFixed(1).padStart(7)}ms`);
  console.log(`  WS    mean ${mean((s) => s.wsMs).toFixed(1).padStart(7)}ms`);
  console.log(
    `  TOTAL mean ${mean((s) => s.totalMs).toFixed(1).padStart(7)}ms   ` +
      `min ${totals[0].toFixed(1)}  p50 ${p50.toFixed(1)}  max ${totals[totals.length - 1].toFixed(1)}`
  );
}

socket.disconnect();
process.exit(0);
