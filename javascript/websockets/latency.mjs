// Measure how long it takes to place an order and see it on the book.
//
//   node javascript/websockets/latency.mjs
//   node javascript/websockets/latency.mjs --rounds 10 --market <market id or symbol>
//
// Picks a market with a resting book, subscribes to that market's socket topic,
// then repeats a round --rounds times (5 by default, capped at 10). Each round
// has two legs:
//
//   place    POST   /api/v1/orders        rest a buy limit 10c under the touch
//                                         (`price` is a dollar string: "0.51")
//   cancel   DELETE /api/v1/orders/<id>   take that same order back off
//
// Both legs are timed the same way - fire the HTTP call, then wait for the order
// book push that reflects it - and print one row each:
//
//   ROUND   which round, 1..--rounds
//   LEG     place or cancel, as above
//   REST    the HTTP round trip - request out, response in
//   WS      from that response to the socket push that shows the change
//   TOTAL   REST + WS, which is what your quoting loop actually sees
//
// The summary at the end averages all three columns over every leg, place and
// cancel together, and adds min, median and max of TOTAL.
//
// The order rests 10c below the best bid rather than crossing it: a fill would
// measure the matching engine instead of the book publish, and would leave a
// position behind. Every round cancels what it placed, so nothing is left open.
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
import {
  loadProfile, signedHeaders, parseArgs, fail, SOCKET_PATH, toNumber, dollarString, fmtMoney,
  unreachable,
} from "../stx.mjs";

const BOOK_TIMEOUT_MS = 15_000;

// Every round puts a real order on a real book and takes it off again. This is a
// measurement, not a load test, and 10 rounds is already 20 legs - well past the
// point where more samples tell you anything new about your own network path.
const MAX_ROUNDS = 10;
const DEFAULT_ROUNDS = 5;

// Printed on every run, to stderr alongside the profile line, so that the table
// below explains itself to someone who never opens this file. stderr keeps it
// out of the way when the numbers are piped somewhere.
const BANNER = `
What this measures: how long from sending an order to seeing it on the book.
Each round has two legs, both timed the same way - fire the HTTP call, then
wait for the order book push that reflects it.

  place    POST   /api/v1/orders        buy limit, 10c under the best bid
  cancel   DELETE /api/v1/orders/<id>   that same order, taken back off

  REST     the HTTP round trip - request out, response in
  WS       from that response to the socket push that shows the change
  TOTAL    REST + WS - what your quoting loop actually sees

These are REAL orders. They rest below the touch so they do not fill, and every
round cancels what it placed.
`;

const args = parseArgs();
const config = loadProfile(args.profile);
const rawRounds = args.rounds ?? DEFAULT_ROUNDS;
let rounds = Number(rawRounds);

// Number.isInteger is false for 2.7 and for NaN alike, so this one check covers
// both a fractional --rounds and a non-numeric one. Python refuses both at parse
// time via argparse type=int; matching that means exiting rather than guessing.
if (!Number.isInteger(rounds)) {
  fail(`--rounds: invalid int value: '${rawRounds}'`);
}


if (config.environment === "production" && !args["force-production"]) {
  fail(
    `Refusing to place orders against production.\n` +
      `Profile [${config.profile}] points at ${config.baseUrl}.\n` +
      `Run this against an integration profile, or pass --force-production if you\n` +
      `really mean to put real orders on a real book.`
  );
}

console.error(`[${config.profile} -> ${config.baseUrl}]`);
console.error(BANNER);

// Printed here, after the banner, so it lands directly above the "N round(s)"
// line it explains. Above the banner it scrolls out of view.
//
// Zero or negative is a well-formed integer, so it gets a default rather than an
// error. Left alone it would run no legs and exit silently, which reads as a
// hang rather than as bad input.
if (rounds < 1) {
  console.error(
    `NOTE: --rounds ${rounds} is not a number of rounds; using ${DEFAULT_ROUNDS}.\n`
  );
  rounds = DEFAULT_ROUNDS;
} else if (rounds > MAX_ROUNDS) {
  console.error(
    `NOTE: --rounds ${rounds} capped at ${MAX_ROUNDS}. Each round places and ` +
      `cancels a real order on a real book.\n`
  );
  rounds = MAX_ROUNDS;
}

// ---------------------------------------------------------------------------
// REST
// ---------------------------------------------------------------------------

async function rest(method, path, body) {
  let response;
  try {
    response = await fetch(config.baseUrl + path, {
      method,
      headers: {
        ...signedHeaders(config, method, path),
        "Content-Type": "application/json",
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (error) {
    // fetch rejects rather than resolving when the host never answered.
    fail(unreachable(config.baseUrl, error));
  }
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

// REST money is a dollar string. Parse before doing arithmetic: `"0.61" - 0.1`
// happens to work by coercion, but `"0.61" + 0.1` is the string "0.610.1".
const bestBid = toNumber(market.bids[0].price);

// Well under the touch so it rests rather than fills. A fill would measure the
// matching engine instead of the book publish, and would leave a position
// behind.
const price = Math.max(0.01, bestBid - 0.1);

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

// One public topic for every market, narrowed by the join payload. At least one
// valid market_id is required: an empty list is a join error, not a
// subscription to everything.
const channel = socket.channel("orderbook", { market_ids: [market.market_id] });

// `unmatched topic` is the server saying it has never heard of the topic, which
// on a correct client means the host predates it. Worth naming, because it looks
// identical to a typo and is the single most likely failure while the
// dollar-format topics are still rolling out. python/websockets/latency.py says
// the same thing on the same failure.
const UNMATCHED_TOPIC_HINT = `
  'unmatched topic' means the server does not know this topic.
  The dollar-format topics (orderbook, ticker, trades, orders:, fills:,
  positions:, settlements:, balances:, account:) need a host running them;
  older deployments carry only the legacy cents topics. See CHANNELS.md.
`;

await new Promise((resolve, reject) => {
  channel
    .join()
    .receive("ok", resolve)
    .receive("error", reject);
}).catch((reason) =>
  fail(
    `could not join orderbook: ${JSON.stringify(reason)}` +
      (reason?.reason === "unmatched topic" ? `\n${UNMATCHED_TOPIC_HINT}` : "")
  )
);

/**
 * Resolve on the next `book` push.
 *
 * The book publishes on the server's own cadence - roughly every 200 ms - and
 * coalesces changes in between. So the WS figure below is dominated by where in
 * that window the order landed, not by network time. It is the number that
 * matters for a quoting loop even so: it is how long until you can see your own
 * order on the book you are quoting from.
 */
function nextBookUpdate() {
  return new Promise((resolve, reject) => {
    const ref = channel.on("book", () => {
      clearTimeout(timer);
      channel.off("book", ref);
      resolve();
    });
    const timer = setTimeout(() => {
      channel.off("book", ref);
      reject(new Error(`no book update within ${BOOK_TIMEOUT_MS}ms`));
    }, BOOK_TIMEOUT_MS);
  });
}

// ---------------------------------------------------------------------------
// Rounds
// ---------------------------------------------------------------------------

console.log(
  `${market.symbol}  best bid ${fmtMoney(bestBid)}, quoting ${fmtMoney(price)}, ${rounds} round(s)`
);
console.log(
  `${"ROUND".padEnd(7)} ${"LEG".padEnd(8)} ${"REST".padStart(9)} ${"WS".padStart(9)} ${"TOTAL".padStart(9)}`
);

// No settle wait here: channel.join() above already resolved on the join reply,
// which is all the settling needed. `orderbook` sends no book on join, and a
// `book` push fires only when the book CHANGES, so waiting for one would just
// burn BOOK_TIMEOUT_MS on any quiet market.

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
          // A string, in dollars. The number 51 is a 400 - see ../stx.mjs.
          price: dollarString(price),
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
