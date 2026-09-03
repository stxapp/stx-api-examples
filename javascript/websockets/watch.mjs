// STX socket watcher - one connection, the book plus all of your own activity.
//
// Run this in one console and trade in another:
//
//   node javascript/websockets/watch.mjs
//   node javascript/websockets/watch.mjs --market <market_id_or_symbol>
//   node javascript/websockets/watch.mjs --cancel-on-disconnect
//
//   # in a second console
//   node javascript/rest/quickstart.mjs roundtrip
//
// Needs `./install.sh` (or `npm install` in javascript/) for the phoenix and ws
// packages. The REST examples need neither.
//
// This uses the official `phoenix` npm client rather than talking the channel
// protocol by hand. It is worth the dependency: it owns join_ref bookkeeping,
// the socket heartbeat, and rejoining every topic after a reconnect. Doing
// those three by hand is where hand-rolled clients quietly go wrong.
// python/websockets/watch.py is the same thing written against the raw frames,
// if you want to see what the client is doing for you.

import { Socket } from "phoenix";
import WebSocket from "ws";
import {
  loadProfile, signedHeaders, parseArgs, argList, fail, SOCKET_PATH, fmtMoney,
  unreachable,
} from "../stx.mjs";

// Two independent timers, and the socket heartbeat only resets one of them.
//
//   socket keep-alive     phx heartbeat on the `phoenix` topic   60s
//   cancel_on_disconnect  `ping` on the orders: topic            what you asked for
//
// Miss the first and the connection closes, which is at least obvious. Miss the
// second and your flagged orders are cancelled on a connection that is still
// up, which is not. phoenix handles the first; the second is ours, below.
const HEARTBEAT_MS = 20_000;

// ping_timeout is clamped server-side to 5000-20000 ms. Values outside that are
// silently pulled to the nearest bound, and a non-integer fails the join with
// {ping_timeout: "Must be an integer"}.
const DEFAULT_PING_TIMEOUT_MS = 10_000;

const args = parseArgs();
const config = loadProfile(args.profile);
const cancelOnDisconnect = Boolean(args["cancel-on-disconnect"]);
const pingTimeoutMs = Number(args["ping-timeout"] ?? DEFAULT_PING_TIMEOUT_MS);

console.error(`[${config.profile} -> ${config.baseUrl}]`);

// ---------------------------------------------------------------------------
// REST calls made once at startup: who we are, and what to watch.
// ---------------------------------------------------------------------------

async function restGet(path) {
  let response;
  try {
    response = await fetch(config.baseUrl + path, {
      headers: signedHeaders(config, "GET", path),
    });
  } catch (error) {
    // fetch rejects rather than resolving when the host never answered.
    fail(unreachable(config.baseUrl, error));
  }
  if (!response.ok) {
    fail(`GET ${path} -> HTTP ${response.status}: ${(await response.text()).slice(0, 300)}`);
  }
  return response.json();
}

// The user id in every private topic comes from here and nowhere else.
const { me } = await restGet("/api/v1/me");
const userId = me.user_id;

// Always the market as the API describes it, never a stub built from --market.
// `orderbook` takes UUIDs only - it drops anything that is not one and then
// rejects the join with `market_ids_required` - and `ticker` needs the market's
// `sport` and `competition` to narrow on. Neither can be had from a symbol
// without asking.
const { markets } = await restGet("/api/v1/markets?status=open&limit=200");
const live = markets.filter((m) => m.trading && m.status === "open");
if (live.length === 0) fail("No tradeable market to watch.");

let market;
if (args.market) {
  market = live.find((m) => m.market_id === args.market || m.symbol === args.market);
  if (!market) fail(`Market ${args.market} is not open and tradeable right now.`);
} else {
  market = live.reduce((best, m) =>
    (m.bids?.length ?? 0) + (m.offers?.length ?? 0) >
    (best.bids?.length ?? 0) + (best.offers?.length ?? 0)
      ? m
      : best
  );
}

// ---------------------------------------------------------------------------
// The socket
// ---------------------------------------------------------------------------

// The handshake signs GET against /socket/websocket with the query string
// DROPPED - phoenix appends ?vsn=2.0.0 itself, and it is not in the signed
// message. Phoenix's transport only surfaces x-* headers, so the names must be
// exact or the socket connects without credentials and then fails on the
// first private channel with "unauthorized".
//
// Signing inside the constructor rather than once up front matters. phoenix
// builds a fresh transport for every reconnect, so this re-signs each time; a
// header computed at startup would be minutes stale by the first reconnect and
// every attempt would fail the 30-second window.
class SignedTransport extends WebSocket {
  constructor(url) {
    super(url, { headers: signedHeaders(config, "GET", SOCKET_PATH) });
  }
}

const socket = new Socket(config.socketEndpoint, {
  transport: SignedTransport,
  heartbeatIntervalMs: HEARTBEAT_MS,
});

socket.onError((error) => console.error("socket error:", error?.message ?? error));
socket.onClose(() => console.error("socket closed"));

socket.connect();

const stamp = () => new Date().toISOString().slice(11, 19);

// Every stamped line goes through `line()` so BOOK, MARKET and the event rows
// share one column. The separator is a space of its own rather than padding,
// because a label exactly LABEL_WIDTH wide would otherwise run into the text:
// USER_INFO, reachable via --topic, is 9 characters.
const LABEL_WIDTH = 9;

// `unmatched topic` is the server saying it has never heard of the topic, which
// on a correct client means the host predates it. Worth naming, because it looks
// identical to a typo and is the single most likely failure while the
// dollar-format topics are still rolling out. Printed once, however many topics
// are missing: on an older host they all fail the same way.
const UNMATCHED_TOPIC_HINT = `
  'unmatched topic' means the server does not know that topic.
  The dollar-format topics this watcher joins need a host running them;
  older deployments carry only the legacy cents topics. See CHANNELS.md.
`;
let warnedUnmatched = false;

function joinFailed(topic, reason) {
  console.error(`could not join ${topic}: ${JSON.stringify(reason)}`);
  if (reason?.reason === "unmatched topic" && !warnedUnmatched) {
    console.error(UNMATCHED_TOPIC_HINT);
    warnedUnmatched = true;
  }
}

/** One output row: time, label column, then whatever the caller has. */
const line = (label, rest) => `${stamp()}  ${label.padEnd(LABEL_WIDTH)} ${rest}`;

// ---------------------------------------------------------------------------
// The book. Every price here is a dollar string, as on /api/v1.
//
// Levels are flat: `bids` and `offers` hold `price`, `quantity`, `liquidity`,
// `total_quantity` and `total_liquidity`. The legacy `market:` topic nested
// them under `ob.b`/`ob.o` with `p`/`q` keys.
//
// Every push is a COMPLETE snapshot of that market's book, not a delta. Replace
// whatever you hold for this market_id rather than merging into it.
// ---------------------------------------------------------------------------

function renderBook(payload) {
  const bids = payload?.bids ?? [];
  const offers = payload?.offers ?? [];
  const bid = bids[0]
    ? `${String(bids[0].quantity).padStart(7)} @ ${fmtMoney(bids[0].price).padStart(6)}`
    : " ".repeat(16);
  const offer = offers[0]
    ? `${fmtMoney(offers[0].price).padEnd(6)} @ ${String(offers[0].quantity).padEnd(7)}`
    : "";
  console.log(line("BOOK", `${bid}   |   ${offer}   (${bids.length}x${offers.length} levels)`));
}

// `orderbook` is ONE public topic covering every market, narrowed by the
// market_ids in the join payload - ten markets is one join, not ten. It is also
// the only topic here whose payload is required: a join naming no usable
// market_id is rejected with {reason: "market_ids_required"} rather than
// quietly subscribing you to every book on the exchange.
const bookChannel = socket.channel("orderbook", { market_ids: [market.market_id] });
bookChannel
  .join()
  // No book arrives on join - the first `book` push comes on the next tick. Use
  // GET /api/v1/markets for the opening state if you need it immediately. The
  // reply echoes the markets actually applied, so a mistyped id shows up here
  // rather than as silence.
  .receive("ok", (response) => {
    console.log(`joined orderbook for ${market.symbol} (${response?.selected_market_ids?.length ?? 0} market(s))`);
  })
  .receive("error", (reason) => {
    joinFailed("orderbook", reason);
    process.exit(1);
  });

bookChannel.on("book", renderBook);

// ---------------------------------------------------------------------------
// The market summary
//
// `ticker` is the other public topic, and it narrows differently: it takes only
// `sports` and `competitions`, with no market_ids, so pinning it to one market
// means filtering client-side as well. Both halves are below - the difference
// between the two feeds is worth seeing.
//
// The filter values are passed through from the REST market verbatim: the
// ticker payload's `sport` and `competition` come from the same server-side
// field, so they match without any normalising.
// ---------------------------------------------------------------------------

const tickerFilter = {};
if (market.sport) tickerFilter.sports = [market.sport];
if (market.competition) tickerFilter.competitions = [market.competition];

const tickerChannel = socket.channel("ticker", tickerFilter);
tickerChannel
  .join()
  // The echo is the only signal that a filter applied. A value the server did
  // not recognise is dropped in silence and comes back as null, meaning no
  // filter at all.
  .receive("ok", (response) =>
    console.log(
      `joined ticker, sports=${JSON.stringify(response?.selected_sports ?? null)} ` +
        `competitions=${JSON.stringify(response?.selected_competitions ?? null)}`
    )
  )
  .receive("error", (reason) => joinFailed("ticker", reason));

/**
 * One `ticker` push: a whole-market price summary, not a book.
 *
 * Complete every time rather than a diff, so there is nothing to merge. It
 * fires when price, the top of book, volume or open interest moves - which
 * includes a new resting level, so an order placed under the touch shows up
 * here as a depth change even though it did not move the price.
 *
 * Any field can be null on a market that has not traded or has an empty side.
 */
tickerChannel.on("ticker", (payload) => {
  // One global topic: every market in the sport arrives here, so the market
  // filter that `orderbook` did server-side has to be done by hand.
  if (payload?.market_id !== market.market_id) return;

  const shown = (key) => (payload[key] === null || payload[key] === undefined ? "-" : payload[key]);
  console.log(
    line(
      "MARKET",
      `last=${fmtMoney(payload.last_traded_price)}  vol=${shown("total_volume")}  ` +
        `oi=${shown("open_interest")}  ${shown("bid_depth")}x${shown("offer_depth")}`
    )
  );
});

// ---------------------------------------------------------------------------
// Your own activity
// ---------------------------------------------------------------------------

const INTERESTING = [
  "id", "status", "price", "quantity", "filled", "action", "market_id",
  "client_order_id", "available_balance", "total_liability", "contracts",
  "cancellation_reason", "rejection_reason",
];

function renderEvent(label, event, payload) {
  // Snapshot events arrive once on join and can carry hundreds of rows.
  for (const key of ["orders", "trades", "positions", "settlements"]) {
    if (Array.isArray(payload?.[key])) {
      console.log(line(label, `${event}: ${payload[key].length} row(s)`));
      return;
    }
  }

  const fields = INTERESTING.filter((key) => payload?.[key] !== undefined)
    .map((key) => `${key}=${payload[key]}`)
    .join("  ");

  console.log(
    line(label, `${event}  `) +
      (fields || JSON.stringify(payload).slice(0, 200))
  );
}

// The dollar topics keep the legacy event names, so a client that already
// handles active_orders needs no re-tagging when it moves to orders:.
//
// The server sends `updated_positions`, not `new_positions`. channel.on()
// matches exactly, so the wrong name means the event is dropped in silence.
// Both are bound: an unused name costs nothing.
//
// balances: is the one topic whose events differ from its legacy twin - it
// joins with `balances`, not portfolio:'s `summary`, and carries no gaming
// fields. `update` and `payment_update` then arrive as they always did.
const PRIVATE_CHANNELS = [
  ["ORDER", `orders:${userId}`, ["new_open_order", "all_orders"]],
  ["FILL", `fills:${userId}`, ["trade", "all_trades"]],
  ["POS", `positions:${userId}`, ["updated_positions", "new_positions", "all_positions"]],
  // settlements: sends no join snapshot; new_settlements arrives as they realise.
  ["SETTLE", `settlements:${userId}`, ["new_settlements"]],
  ["WALLET", `balances:${userId}`, ["balances", "update", "payment_update"]],
];

// Anything passed with --topic joins on the same socket. These have no known
// event names, so bind nothing and let the catch-all below print whatever
// arrives, the way watch.py does for every topic.
const EXTRA_CHANNELS = argList(args.topic).map((topic) => [
  topic.split(":")[0].toUpperCase(),
  topic.replace("<user_id>", userId),
  [],
]);

for (const [label, topic, events] of [...PRIVATE_CHANNELS, ...EXTRA_CHANNELS]) {
  const isOrders = topic.startsWith("orders:");

  // cancel_on_disconnect is negotiated in the join payload of the orders:
  // channel, and nowhere else. The `account:` aggregate does not support it at
  // all - see CHANNELS.md.
  const joinPayload =
    isOrders && cancelOnDisconnect
      ? { cancel_on_disconnect: true, ping_timeout: pingTimeoutMs }
      : {};

  const channel = socket.channel(topic, joinPayload);

  channel
    .join()
    .receive("ok", (response) => {
      if (isOrders && cancelOnDisconnect) {
        // The server echoes the timeout it actually chose after clamping. Use
        // that, not the value you asked for.
        const negotiated = response?.ping_timeout ?? pingTimeoutMs;
        console.log(`joined ${topic} with cancel_on_disconnect, ping_timeout=${negotiated}ms`);

        // On a timer at half the negotiated timeout, not in response to
        // traffic: a quiet market produces no traffic and the deadline does not
        // care. The reply is {ping: "pong", ttl: <ping_timeout>}.
        const timer = setInterval(
          () => channel.push("ping", {}),
          Math.max(1000, Math.floor(negotiated / 2))
        );
        timer.unref?.();
      } else {
        console.log(`joined ${topic}`);
      }
    })
    .receive("error", (reason) => joinFailed(topic, reason));

  if (events.length === 0) {
    // channel.on() needs a name. onMessage sees every frame on this topic,
    // which is what an unknown topic needs.
    channel.onMessage = (event, payload) => {
      if (!event.startsWith("phx_") && !event.startsWith("chan_")) {
        renderEvent(label, event, payload);
      }
      return payload;
    };
  }
  for (const event of events) {
    channel.on(event, (payload) => renderEvent(label, event, payload));
  }
}

console.log(
  // orderbook and ticker, plus the private topics and anything from --topic.
  `\nwatching ${market.symbol}  (${2 + PRIVATE_CHANNELS.length + EXTRA_CHANNELS.length} channels)` +
    (cancelOnDisconnect ? `, cancel_on_disconnect on` : "") +
    `   ctrl-c to stop\n`
);

process.on("SIGINT", () => {
  socket.disconnect();
  console.log("\nstopped");
  process.exit(0);
});
