// Quote every combo request as a maker, over a plain Phoenix socket.
//
//   node javascript/websockets/combo_maker.mjs
//   node javascript/websockets/combo_maker.mjs --win-multiple 12 --cash-out-price 0.10
//   node javascript/websockets/combo_maker.mjs --frames      # also print every frame
//
// Players ask for quotes on a combo: a set of legs and a stake to buy, or a
// combo they hold to cash out. Every maker joined to its combo_quote:<user_id>
// topic receives each request as `rfq_opened`. This script quotes all of them
// and prints what happens next, until Ctrl-C:
//
//   [won]      quote_accepted, then placed: your quote was taken
//   [lost]     negotiation_closed placed_elsewhere: another maker's was
//   [expired]  negotiation_closed expired: your quote ran out
//   [closed]   rfq_closed: the request ended, for any reason
//
// On a buy you quote what the player wins: the stake times --win-multiple. On a
// cash out you quote what you pay for everything the player sells:
// --cash-out-price a contract. Each quote stands for --quote-seconds.
//
// Combos need a read_write key: a read_only key is refused at the join.
// CHANNELS.md has every frame this sends and receives. Like watch_channel.mjs
// this speaks the raw frames, with only `ws` for the transport.

import WebSocket from "ws";
import { loadProfile, signedHeaders, parseArgs, fail, SOCKET_PATH, unreachable } from "../stx.mjs";

// The socket closes after 60 s without a heartbeat, so send one every 30.
const HEARTBEAT_MS = 30_000;

// Seconds to wait before reconnecting, doubling up to the last value.
const BACKOFF = [1, 2, 5, 10, 30];

const USAGE = `usage: combo_maker.mjs [--profile PROFILE] [--win-multiple N] [--cash-out-price P]
                       [--quote-seconds S] [--seconds S] [--frames]

Quote every combo request as a maker.

options:
  --profile PROFILE    profile in ~/.stx/credentials
  --win-multiple N     on a buy, quote the stake times this (default 9)
  --cash-out-price P   on a cash out, pay this a contract (default 0.08)
  --quote-seconds S    how long each quote stands (default 30)
  --seconds S          stop after this many seconds (default: run until Ctrl-C)
  --frames             also print every frame sent and received`;

const args = parseArgs();
if (args.help || args._.includes("-h")) {
  console.log(USAGE);
  process.exit(0);
}
const winMultiple = String(args["win-multiple"] ?? "9");
const cashOutPrice = String(args["cash-out-price"] ?? "0.08");
const quoteMs = Math.round(Number(args["quote-seconds"] ?? 30) * 1000);
const config = loadProfile(args.profile);

const stamp = () => new Date().toISOString().slice(11, 19);
const short = (id) => (id ?? "-").slice(0, 8);

// ---------------------------------------------------------------------------
// The amount to quote
//
// Money is a decimal string, so the arithmetic is on integers of 10^-8 dollars,
// never on floats. The exchange derives a price from the amount, rounded to
// `tick_size` in the maker's favour, and refuses a quote whose price is not
// above 0 and below `max_price` (price_out_of_range), or, on a buy, whose stake
// buys no whole contract (below_one_contract). This checks the same first.
// ---------------------------------------------------------------------------

const SCALE = 10n ** 8n;
const CENT = SCALE / 100n;

function units(value) {
  const [whole, fraction = ""] = String(value).split(".");
  return BigInt(whole) * SCALE + BigInt(fraction.padEnd(8, "0").slice(0, 8));
}

const dollars = (u) => `${u / SCALE}.${String((u % SCALE) / CENT).padStart(2, "0")}`;

function quoteAmount(request) {
  const maxPrice = units(request.max_price);
  const tick = units(request.tick_size);
  if (request.side === "buy") {
    // A buy: what the player wins on their stake.
    const stake = units(request.amount);
    const amount = ((stake * units(winMultiple)) / SCALE / CENT) * CENT;
    if (amount <= 0n) return null;
    const price = ((maxPrice * stake + (stake + amount) * tick - 1n) / ((stake + amount) * tick)) * tick;
    if (price <= 0n || price >= maxPrice || stake / price < 1n) return null;
    return dollars(amount);
  }
  // A cash out: what you pay for the whole quantity.
  const quantity = units(request.quantity);
  const amount = ((quantity * units(cashOutPrice)) / SCALE / CENT) * CENT;
  const price = ((amount * SCALE) / (quantity * tick)) * tick;
  if (price <= 0n || price >= maxPrice) return null;
  return dollars(amount);
}

function describe(request) {
  if (request.side === "buy") {
    const legs = request.legs.map((leg) => `${leg.short_name ?? short(leg.market_id)} ${leg.outcome}`).join(", ");
    return `buy, stake ${request.amount}, ${request.legs.length} legs: ${legs}`;
  }
  return `cash out of ${request.quantity} on combo ${short(request.market_id)}`;
}

// ---------------------------------------------------------------------------
// The topic is your own user id, from GET /api/v1/me.
// ---------------------------------------------------------------------------

let me;
try {
  const response = await fetch(config.baseUrl + "/api/v1/me", { headers: signedHeaders(config, "GET", "/api/v1/me") });
  if (!response.ok) fail(`GET /api/v1/me -> HTTP ${response.status}`);
  ({ me } = await response.json());
} catch (error) {
  fail(unreachable(config.baseUrl, error));
}
const topic = `combo_quote:${me.user_id}`;

let ref = 0;
let joins = 0;
let attempt = 0;
let stopping = false;
let current = null;

function connect() {
  // A fresh signature on every connect: the timestamp is good for 30 s.
  const ws = new WebSocket(`${config.socketUrl}?vsn=2.0.0`, { headers: signedHeaders(config, "GET", SOCKET_PATH) });
  current = ws;
  // What each command we sent was, by its ref, so its phx_reply can be explained.
  const pending = new Map();
  // The join's ref becomes the channel's join_ref. Every later frame to the topic must
  // carry it: the exchange silently drops a frame with an old one.
  const joinRef = String(++ref);
  const startedAt = Date.now();
  let heartbeat;

  const send = (frame) => {
    const text = JSON.stringify(frame);
    if (args.frames) console.log(`${stamp()}  -> ${text}`);
    ws.send(text);
  };

  ws.on("open", () => {
    pending.set(joinRef, { kind: "join" });
    send([joinRef, joinRef, topic, "phx_join", {}]);
    // On the `phoenix` topic, with a null join_ref: it belongs to no channel.
    heartbeat = setInterval(() => send([null, String(++ref), "phoenix", "heartbeat", {}]), HEARTBEAT_MS);
  });

  ws.on("message", (data) => {
    if (args.frames) console.log(`${stamp()}  <- ${data}`);
    const [frameJoinRef, frameRef, frameTopic, event, payload] = JSON.parse(data);
    if (frameTopic === "phoenix") return; // heartbeat replies
    const rfq = short(payload.rfq_id);
    switch (event) {
      case "phx_reply":
        reply(pending, frameRef, payload);
        break;
      case "rfq_opened": {
        // Every request opened after the join, from every player, your own included.
        console.log(`[request]  ${rfq}  ${describe(payload)}`);
        const amount = quoteAmount(payload);
        if (amount === null) {
          console.log(`[skip]     ${rfq}  no amount prices inside 0 to ${payload.max_price}`);
          break;
        }
        const quoteRef = String(++ref);
        pending.set(quoteRef, { kind: "quote", request: payload });
        send([joinRef, quoteRef, topic, "quote", {
          rfq_id: payload.rfq_id,
          amount, // a string: a JSON number is refused with malformed_money
          expires_in_ms: quoteMs,
          client_order_id: `maker-${rfq}`,
        }]);
        break;
      }
      case "quote_accepted":
        // The player took your quote; the exchange is placing both orders now.
        console.log(`[accepted] ${rfq}  quote ${payload.quote_id}`);
        break;
      case "placed":
        console.log(`[won]      ${rfq}  order ${payload.order_id}  ${payload.quantity} @ ${payload.price}  combo ${payload.market_id}`);
        break;
      case "negotiation_closed": {
        // One of your quotes stopped standing, once per quote, with the reason.
        const label = { placed_elsewhere: "[lost]   ", expired: "[expired]" }[payload.reason] ?? "[ended]  ";
        console.log(`${label}  ${rfq}  quote ${payload.quote_id}  ${payload.reason}`);
        break;
      }
      case "rfq_closed":
        console.log(`[closed]   ${rfq}  ${payload.reason}`);
        break;
      case "phx_error":
      case "phx_close":
        // The channel ended on a live socket: its quotes are gone. Reconnect.
        if (frameJoinRef === joinRef) {
          console.log(`[channel]  ${event}; reconnecting`);
          ws.terminate();
        }
        break;
    }
  });

  // A 403 from CloudFront here, with an HTML body, means no User-Agent was sent.
  ws.on("error", (error) => console.log(`[socket]   ${error.message}`));

  ws.on("close", () => {
    clearInterval(heartbeat);
    if (stopping) return;
    // Quotes belong to the connection that placed them, so a drop withdrew them.
    // Reconnecting re-signs, rejoins with a fresh join_ref and starts the heartbeat again.
    attempt = Date.now() - startedAt > 60_000 ? 0 : attempt + 1;
    const delay = BACKOFF[Math.min(attempt, BACKOFF.length - 1)];
    console.log(`[socket]   disconnected; quotes were withdrawn. Reconnecting in ${delay} s`);
    setTimeout(connect, delay * 1000);
  });
}

function reply(pending, frameRef, { status, response = {} }) {
  const sent = pending.get(frameRef);
  pending.delete(frameRef);
  if (sent?.kind === "join") {
    if (status !== "ok") {
      // unmatched topic: no combos on this exchange. "This API key does not have write
      // access": a read_only key. Neither changes on a retry.
      fail(`Join refused: ${response.reason}`);
    }
    if (++joins > 1) {
      console.log(`[socket]   rejoined ${topic}, join_ref ${frameRef}`);
      return;
    }
    console.log(`Joined ${topic} on ${config.baseUrl} as a maker.`);
    console.log(`Buys: win ${winMultiple}x the stake. Cash outs: ${cashOutPrice} a contract. Each quote stands ${quoteMs / 1000} s. Ctrl-C to stop.\n`);
  } else if (sent?.kind === "quote") {
    const { request } = sent;
    const rfq = short(request.rfq_id);
    if (status !== "ok") {
      // e.g. cannot_quote_own_request, or not_found when the request ended first
      console.log(`[refused]  ${rfq}  ${response.reason}`);
    } else if (request.side === "buy") {
      console.log(`[quoted]   ${rfq}  win ${response.amount}  quote ${response.quote_id}`);
    } else {
      console.log(`[quoted]   ${rfq}  pay ${response.amount} for ${request.quantity}  quote ${response.quote_id}`);
    }
  }
}

function stop() {
  stopping = true;
  current?.close();
  console.log("\nStopped. Quotes still standing were withdrawn with the connection.");
  process.exit(0);
}

process.on("SIGINT", stop);
if (args.seconds) setTimeout(stop, Number(args.seconds) * 1000);
connect();
