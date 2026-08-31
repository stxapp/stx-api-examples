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
import { loadProfile, signedHeaders, parseArgs, fail, SOCKET_PATH, bookPriceCents } from "../stx.mjs";

// Two independent timers, and the socket heartbeat only resets one of them.
//
//   socket keep-alive     phx heartbeat on the `phoenix` topic   60s
//   cancel_on_disconnect  `ping` on the active_orders topic      what you asked for
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
  const response = await fetch(config.baseUrl + path, {
    headers: signedHeaders(config, "GET", path),
  });
  if (!response.ok) {
    fail(`GET ${path} -> HTTP ${response.status}: ${(await response.text()).slice(0, 300)}`);
  }
  return response.json();
}

// The user id in every private topic comes from here and nowhere else.
const { me } = await restGet("/api/v1/me");
const userId = me.user_id;

let market;
if (args.market) {
  market = { market_id: args.market, symbol: args.market };
} else {
  const { markets } = await restGet("/api/v1/markets?status=open&limit=200");
  const live = markets.filter((m) => m.trading && m.status === "open");
  if (live.length === 0) fail("No tradeable market to watch.");
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

// ---------------------------------------------------------------------------
// The book. Every price here is integer cents.
// ---------------------------------------------------------------------------

function renderBook(payload) {
  const bids = payload?.ob?.b ?? [];
  const offers = payload?.ob?.o ?? [];
  const bid = bids[0] ? `${String(bids[0].q).padStart(6)} @ ${String(bookPriceCents(bids[0].p)).padStart(4)}c` : " ".repeat(14);
  const offer = offers[0] ? `${`${bookPriceCents(offers[0].p)}c`.padEnd(5)} @ ${String(offers[0].q).padEnd(6)}` : "";
  console.log(`${stamp()}  BOOK   ${bid}   |   ${offer}   (${bids.length}x${offers.length} levels)`);
}

const bookChannel = socket.channel(`market:${market.market_id}`, {});
bookChannel
  .join()
  // A market: join replies with the current book, so there is no need to ask
  // for a snapshot before the first push arrives.
  .receive("ok", (response) => {
    console.log(`joined market:${market.symbol}`);
    if (response?.ob) renderBook(response);
  })
  .receive("error", (reason) => fail(`could not join the market channel: ${JSON.stringify(reason)}`));

bookChannel.on("order_book_update", renderBook);
bookChannel.on("market_update", (payload) =>
  console.log(`${stamp()}  MARKET status=${payload.status} trading=${payload.trading}`)
);

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
      console.log(`${stamp()}  ${label.padEnd(8)}${event}: ${payload[key].length} row(s)`);
      return;
    }
  }

  const fields = INTERESTING.filter((key) => payload?.[key] !== undefined)
    .map((key) => `${key}=${payload[key]}`)
    .join("  ");

  console.log(
    `${stamp()}  ${label.padEnd(8)}${event}  ` +
      (fields || JSON.stringify(payload).slice(0, 200))
  );
}

const PRIVATE_CHANNELS = [
  ["ORDER", `active_orders:${userId}`, ["new_open_order", "all_orders"]],
  ["TRADE", `active_trades:${userId}`, ["new_trade", "all_trades"]],
  ["POS", `active_positions:${userId}`, ["new_positions", "all_positions"]],
  ["SETTLE", `active_settlements:${userId}`, ["new_settlements", "all_settlements"]],
  ["WALLET", `portfolio:${userId}`, ["portfolio_update", "summary"]],
];

for (const [label, topic, events] of PRIVATE_CHANNELS) {
  const isOrders = topic.startsWith("active_orders:");

  // cancel_on_disconnect is negotiated in the join payload of the active_orders
  // channel, and nowhere else.
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
    .receive("error", (reason) =>
      console.error(`could not join ${topic}: ${JSON.stringify(reason)}`)
    );

  for (const event of events) {
    channel.on(event, (payload) => renderEvent(label, event, payload));
  }
}

console.log(
  `\nwatching ${market.symbol} and ${PRIVATE_CHANNELS.length} private channels` +
    (cancelOnDisconnect ? `, cancel_on_disconnect on` : "") +
    `   ctrl-c to stop\n`
);

process.on("SIGINT", () => {
  socket.disconnect();
  console.log("\nstopped");
  process.exit(0);
});
