// STX REST quickstart - signed requests, market data, and an order round trip.
//
//   node javascript/rest/quickstart.mjs me           # who this key belongs to
//   node javascript/rest/quickstart.mjs markets      # markets with a resting book
//   node javascript/rest/quickstart.mjs orders       # your open orders
//   node javascript/rest/quickstart.mjs roundtrip    # place a resting order, then cancel it
//
// Add --profile <name> to use a profile other than [default]:
//
//   node javascript/rest/quickstart.mjs --profile ca-integration markets
//
// ZERO DEPENDENCIES. Node has Ed25519 in node:crypto and fetch built in, so
// there is nothing to npm install for this file - `./install.sh` is only needed
// for the WebSocket examples. Node 20 or newer.
//
// Credentials come from ~/.stx/credentials, written by ./configure. Every
// /api/v1 route requires a signature, so even `markets` needs a key. `roundtrip` needs a read_write one.

import {
  loadProfile, signedHeaders, parseArgs, fail, toNumber, dollarString, fmtMoney,
  unreachable,
} from "../stx.mjs";

/**
 * Send one signed request and return the decoded JSON.
 *
 * The signature covers the method and the path INCLUDING any query string, so
 * the path is built before signing and then used verbatim. Signing
 * /api/v1/markets and sending /api/v1/markets?status=open is a 401.
 */
async function request(config, method, path, body) {
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

  const text = await response.text();

  if (response.status === 401) {
    fail(
      `401 unauthorized.\n` +
        `  The signature, key id or timestamp was rejected. The most common causes:\n` +
        `  - the key belongs to a different environment than ${config.exchange}/${config.environment}\n` +
        `  - the machine clock is more than 30 seconds off\n` +
        `  Body: ${text.slice(0, 300)}`
    );
  }

  if (!response.ok) {
    // 400 is a malformed request. On POST /api/v1/orders the usual cause is a
    // `price` sent as a number instead of a dollar string - see cmdRoundtrip.
    // 422 is the exchange rejecting a well-formed, authenticated request: a
    // price at or above the market's max_price, say. Read either in full.
    fail(`HTTP ${response.status}: ${text.slice(0, 500)}`);
  }

  return JSON.parse(text);
}

// ---------------------------------------------------------------------------
// Identity
// ---------------------------------------------------------------------------

/**
 * GET /api/v1/me - the only place your user id is published.
 *
 * Private WebSocket topics are scoped by it: orders:<user_id>,
 * balances:<user_id>, account:<user_id>, and so on. Fetch it once at startup
 * and hold it.
 */
async function cmdMe(config) {
  const { me } = await request(config, "GET", "/api/v1/me");
  console.log(`user_id     ${me.user_id}`);
  console.log(`account_id  ${me.account_id}`);
  console.log(`key_id      ${me.key_id}`);
  console.log(`scope       ${me.scope}`);
  if (me.scope === "read_only") {
    console.log("\nThis key cannot place or cancel orders. `roundtrip` needs read_write.");
  }
}

// ---------------------------------------------------------------------------
// Market data
// ---------------------------------------------------------------------------

// The endpoint's ceiling: limit=300 and limit=1000 both come back with 200.
// Omitting limit entirely gives you 100.
const MARKET_PAGE_LIMIT = 200;

/**
 * One page of markets.
 *
 * Collections come back as {cursor, markets: [...]}, not {data: [...]}. Pass
 * the cursor back as ?cursor=... for the next page; it is null on the last.
 *
 * `status` is lowercase: ?status=OPEN is a 400, and `open` is the only value the
 * endpoint accepts. `trading=true` works as a query param too, though the
 * examples filter on the `trading` field below so the raw page stays visible.
 */
async function listMarkets(config, limit = MARKET_PAGE_LIMIT) {
  const { markets } = await request(
    config,
    "GET",
    `/api/v1/markets?status=open&limit=${limit}`
  );
  return markets;
}

/** Markets that are actually quotable right now, deepest book first. */
function tradeable(markets) {
  return markets
    .filter((m) => m.trading && m.status === "open")
    .sort(
      (a, b) =>
        (b.bids?.length ?? 0) + (b.offers?.length ?? 0) -
        ((a.bids?.length ?? 0) + (a.offers?.length ?? 0))
    );
}

async function cmdMarkets(config) {
  const open = await listMarkets(config);
  const markets = tradeable(open);
  if (markets.length === 0) fail("No tradeable markets right now.");

  // Every price here is a dollar string ("0.6100", "1.0000"), so the column is
  // a straight format rather than a conversion. A US market settles at $1, so
  // max_price is "1.0000" and quotes run $0.01-$0.99. Canada settles at $100
  // and max_price is "100.0000". Read it off the market, do not assume.
  // Symbols are back-loaded: the leg that distinguishes sibling markets
  // (TOTAL-3_5 from TOTAL-4_5) is in the tail, and --market takes a symbol, so
  // this column has to survive intact. TITLE is last and absorbs the slack - a
  // narrow terminal costs you title text, never the identifier.
  const shown = markets.slice(0, 15);
  const symbolWidth = Math.max(...shown.map((m) => m.symbol.length));
  const titleWidth = Math.max(20, (process.stdout.columns || 80) - symbolWidth - 26);

  console.log(
    `${"SYMBOL".padEnd(symbolWidth)} ${"BID".padStart(7)} ${"OFFER".padStart(7)} ${"MAX".padStart(7)}  TITLE`
  );
  for (const market of shown) {
    const bid = market.bids?.[0] ? fmtMoney(market.bids[0].price) : "-";
    const offer = market.offers?.[0] ? fmtMoney(market.offers[0].price) : "-";
    console.log(
      `${market.symbol.padEnd(symbolWidth)} ${bid.padStart(7)} ${offer.padStart(7)} ` +
        `${fmtMoney(market.max_price).padStart(7)}  ${(market.title ?? "").slice(0, titleWidth)}`
    );
  }

  console.log(
    `\n${markets.length} tradeable of ${open.length} returned by ` +
      `?status=open&limit=${MARKET_PAGE_LIMIT}.`
  );
}

// ---------------------------------------------------------------------------
// Orders
// ---------------------------------------------------------------------------

async function cmdOrders(config) {
  const { orders } = await request(config, "GET", "/api/v1/orders?status=open");
  if (orders.length === 0) {
    console.log("No open orders.");
    return;
  }

  // price is a dollar string, or null on a market order. quantity and filled
  // are strings too ("1.00"), and print as they arrive - a column needs no
  // conversion. Parse before doing ARITHMETIC on them, as cmdRoundtrip does.
  console.log(
    `${"ID".padEnd(38)} ${"SIDE".padEnd(5)} ${"QTY".padStart(6)} ${"PRICE".padStart(7)} ${"FILLED".padStart(7)}  STATUS`
  );
  for (const order of orders) {
    const price = order.price == null ? "MKT" : fmtMoney(order.price);
    console.log(
      `${order.id.padEnd(38)} ${order.action.padEnd(5)} ${order.quantity.padStart(6)} ` +
        `${price.padStart(7)} ${(order.filled ?? "0.00").padStart(7)}  ${order.status}`
    );
  }
}

/**
 * Place a limit order well away from the touch, then cancel it.
 *
 * Priced so it should rest rather than fill, but this is a real order on a real
 * book: on integration that costs nothing, on production it does not.
 */
async function cmdRoundtrip(config, args) {
  if (config.environment === "production" && !args["force-production"]) {
    fail(
      `Refusing to place orders against production from an example script.\n` +
        `Profile [${config.profile}] points at ${config.baseUrl}.\n` +
        `Pass --force-production if you really mean to.`
    );
  }

  const market = tradeable(await listMarkets(config)).find((m) => m.bids?.length > 0);
  if (!market) fail("No tradeable market with a bid to price against.");

  // Parse before doing arithmetic. `"0.61" - 0.1` happens to work by coercion,
  // but `"0.61" + 0.1` is the string "0.610.1" and nothing warns you.
  const bestBid = toNumber(market.bids[0].price);

  // Ten cents under the touch, floored at a cent. The ceiling is the market's
  // own max_price, and going over it is a 422 quoting the cap.
  const price = Math.max(0.01, bestBid - 0.1);

  console.log(
    `${market.symbol}  best bid ${fmtMoney(bestBid)}, max_price ${fmtMoney(market.max_price)}`
  );
  console.log(`placing    BUY 1 @ ${fmtMoney(price)}`);

  // The body is flat. Wrapping it in {user_order: {...}} returns 400. And a
  // successful placement is a 200, not a 201.
  const { order } = await request(config, "POST", "/api/v1/orders", {
    market_id: market.market_id,
    order_type: "limit",
    action: "buy",
    // A STRING, in dollars. Sending the number 51 is a 400: an integer used to
    // mean 51 cents, and reading it as $51.00 would be a 100x overprice, so the
    // server rejects it rather than guessing. quantity is exempt - a contract
    // count carries no unit ambiguity - and still takes a number.
    price: dollarString(price),
    quantity: 1,
    // Your own reference, echoed back on the order and on every socket push
    // about it. Use it to tie exchange state to your own.
    client_order_id: `quickstart-${Date.now()}`,
  });
  // The echoed price is the same order at the server's width: "0.51" in,
  // "0.5100" back. Compare as numbers, never as strings.
  console.log(
    `placed     ${order.id}  status=${order.status}  price=${order.price}  filled=${order.filled ?? "0.00"}`
  );

  const cancelled = await request(config, "DELETE", `/api/v1/orders/${order.id}`);
  console.log(`cancelled  status=${cancelled.status}`);
}

const COMMANDS = {
  me: cmdMe,
  markets: cmdMarkets,
  orders: cmdOrders,
  roundtrip: cmdRoundtrip,
};

const args = parseArgs();
const command = args._[0] ?? "me";

if (!COMMANDS[command]) {
  fail(`Unknown command "${command}". One of: ${Object.keys(COMMANDS).join(", ")}`);
}

const config = loadProfile(args.profile);
console.error(`[${config.profile} -> ${config.baseUrl}]\n`);

await COMMANDS[command](config, args);
