// STX GraphQL quickstart - the same API key, against /api/graphql.
//
//   node javascript/graphql/quickstart.mjs markets    # public market metadata
//   node javascript/graphql/quickstart.mjs orders     # your own orders
//
// Add --profile <name> to use a profile other than [default].
//
// ZERO DEPENDENCIES. Node has Ed25519 in node:crypto and fetch built in.
//
// GraphQL takes API-key signing, exactly as REST does: the server checks
// X-STX-ACCESS-* first and only falls back to a JWT bearer token when those
// headers are absent. So one signing implementation covers both transports, and
// the only thing that changes is the path you sign - here always
//
//   <timestamp>POST/api/graphql
//
// because the operation travels in the body and the body is not signed.
//
// Which to use? REST is the documented surface and what the /api/v1 examples in
// this repository use. GraphQL is not legacy - it is where the richer market
// queries live, and it will be documented publicly later. Two caveats worth
// knowing before you mix them:
//
//   1. Money is not necessarily represented the same way on both. REST is
//      integer cents throughout; the GraphQL schema serialises some money
//      fields differently. Do not carry a number from one to the other without
//      checking what it is.
//   2. The GraphQL schema is not published yet, so introspect the endpoint
//      (/graphiql on an integration host) rather than working from a copy.

import { loadProfile, signedHeaders, parseArgs, fail, GRAPHQL_PATH } from "../stx.mjs";

async function graphql(config, query, variables = {}) {
  const response = await fetch(config.baseUrl + GRAPHQL_PATH, {
    method: "POST",
    headers: {
      // The path is the constant /api/graphql. The body carries the operation
      // and is not part of the signed message.
      ...signedHeaders(config, "POST", GRAPHQL_PATH),
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ query, variables }),
  });

  if (response.status === 401) {
    fail(
      `401 unauthorized. The key, signature or timestamp was rejected - check the key\n` +
        `belongs to ${config.exchange}/${config.environment} and the clock is within 30s.`
    );
  }

  const body = await response.json();

  // GraphQL answers 200 with an `errors` array rather than an HTTP error code,
  // so a status check alone will happily hand you an empty result. Always look
  // at `errors`. An auth failure surfaces here as "unauthorized", not as a 401.
  if (body.errors) {
    fail(`GraphQL errors:\n${JSON.stringify(body.errors, null, 2)}`);
  }

  return body.data;
}

const MARKETS_QUERY = `
  query Markets($input: MarketInfosInput) {
    marketInfos(input: $input) {
      marketId
      symbol
      title
      status
      trading
      sport
      competition
    }
  }
`;

async function cmdMarkets(config) {
  const { marketInfos } = await graphql(config, MARKETS_QUERY, {
    input: { status: ["OPEN"], limit: 15 },
  });

  if (!marketInfos?.length) {
    console.log("No open markets returned.");
    return;
  }

  console.log(`${"SYMBOL".padEnd(28)} ${"STATUS".padEnd(10)} ${"TRADING".padEnd(8)} TITLE`);
  for (const market of marketInfos) {
    console.log(
      `${(market.symbol ?? "").slice(0, 28).padEnd(28)} ${String(market.status).padEnd(10)} ` +
        `${String(market.trading).padEnd(8)} ${(market.title ?? "").slice(0, 40)}`
    );
  }
  console.log(`\n${marketInfos.length} markets. Same data as GET /api/v1/markets, different shape.`);
}

const ORDERS_QUERY = `
  query MyOrders($status: OrderStatus) {
    myOrderHistory(status: $status) {
      totalCount
      orders {
        id
        marketId
        action
        orderType
        quantity
        filled
        status
        clientOrderId
      }
    }
  }
`;

async function cmdOrders(config) {
  // A signed read. Nothing about the request differs from the public query
  // above - the same three headers go on both, because there is no anonymous
  // access to your own orders.
  const { myOrderHistory } = await graphql(config, ORDERS_QUERY, { status: "ACCEPTED" });

  console.log(`${myOrderHistory.totalCount} order(s) in ACCEPTED`);
  for (const order of myOrderHistory.orders ?? []) {
    console.log(
      `  ${order.id}  ${String(order.action).padEnd(5)} ` +
        `${String(order.quantity).padStart(6)} filled=${order.filled ?? 0}  ` +
        `${order.status}  ${order.clientOrderId ?? ""}`
    );
  }
}

const COMMANDS = { markets: cmdMarkets, orders: cmdOrders };

const args = parseArgs();
const command = args._[0] ?? "markets";

if (!COMMANDS[command]) {
  fail(`Unknown command "${command}". One of: ${Object.keys(COMMANDS).join(", ")}`);
}

const config = loadProfile(args.profile);
console.error(`[${config.profile} -> ${config.baseUrl}${GRAPHQL_PATH}]\n`);

await COMMANDS[command](config);
