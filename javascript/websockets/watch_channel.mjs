// Join one channel and print every frame it sends.
//
//   node javascript/websockets/watch_channel.mjs --topic ticker
//   node javascript/websockets/watch_channel.mjs --topic 'orders:<user_id>'
//   node javascript/websockets/watch_channel.mjs --topic 'account:<user_id>'
//
// --full prints whole payloads. They are clipped by default, because a join
// snapshot can carry hundreds of rows and one `ticker` frame is already wider
// than most terminals:
//
//   node javascript/websockets/watch_channel.mjs --topic ticker --full
//
// --payload sets the join payload. The public topics use it for filtering, and
// `orderbook` REQUIRES at least one market_id:
//
//   node javascript/websockets/watch_channel.mjs --topic orderbook \
//     --payload '{"market_ids": ["<market_id>"]}'
//
//   node javascript/websockets/watch_channel.mjs --topic ticker \
//     --payload '{"sports": ["baseball"], "competitions": ["MLB"]}'
//
// account:<user_id> is the one topic with no legacy twin: it carries orders,
// fills, positions, settlements and balances on a single join. Do not join it
// alongside the per-type topics - you would receive everything twice.
//
// watch.mjs joins six channels at once and formats the events it recognises.
// This joins only what you name and prints frames as they arrive, unformatted,
// which is what you want when working through CHANNELS.md one channel at a time.
//
// --topic is repeatable, and <user_id> is substituted from GET /api/v1/me.
//
// Unlike watch.mjs this speaks the raw Phoenix frames rather than using the
// `phoenix` client, because binding a channel needs event names up front and
// the point here is to see whatever arrives. Only `ws` is needed.
//
// Needs `./install.sh` (or `npm install` in javascript/) for ws.

import WebSocket from "ws";
import {
  loadProfile, signedHeaders, parseArgs, argList, fail, SOCKET_PATH, unreachable,
} from "../stx.mjs";

// The socket closes after 60s of silence. This is the socket keep-alive, not
// the cancel_on_disconnect ping, which is a separate timer on orders:;
// see CHANNELS.md. watch.mjs is the example that negotiates that one.
const HEARTBEAT_MS = 20_000;

// Payloads are clipped to this many characters unless --full is given. A join
// snapshot can carry hundreds of rows; one ticker frame is already wider than
// most terminals.
const DEFAULT_MAX_CHARS = 400;

const stamp = () => new Date().toISOString().slice(11, 19);

// The topic's prefix, padded. The full topic is on the join frame; repeating a
// user id on every line only makes them unreadable.
const label = (topic) => topic.split(":")[0].padEnd(18);

const args = parseArgs();
const config = loadProfile(args.profile);
const topics = argList(args.topic);
const maxChars = args.full ? null : DEFAULT_MAX_CHARS;
if (topics.length === 0) {
  fail("--topic is required, e.g. --topic markets");
}

let joinPayload = {};
try {
  joinPayload = JSON.parse(args.payload ?? "{}");
} catch (error) {
  fail(`--payload is not valid JSON: ${error.message}`);
}
if (typeof joinPayload !== "object" || joinPayload === null || Array.isArray(joinPayload)) {
  fail("--payload must be a JSON object");
}

console.error(`[${config.profile} -> ${config.baseUrl}]`);

async function get(path) {
  let response;
  try {
    response = await fetch(config.baseUrl + path, {
      headers: signedHeaders(config, "GET", path),
    });
  } catch (error) {
    // fetch rejects rather than resolving when the host never answered.
    fail(unreachable(config.baseUrl, error));
  }
  if (!response.ok) fail(`GET ${path} -> HTTP ${response.status}`);
  return response.json();
}

// Only fetch the user id when a topic actually needs it, so the market
// channels work without the extra round trip.
let resolved = topics;
if (topics.some((t) => t.includes("<user_id>"))) {
  const { me } = await get("/api/v1/me");
  resolved = topics.map((t) => t.replace("<user_id>", me.user_id));
}

const ws = new WebSocket(`${config.socketUrl}?vsn=2.0.0`, {
  headers: signedHeaders(config, "GET", SOCKET_PATH),
});

ws.on("open", () => {
  resolved.forEach((topic, index) => {
    // [join_ref, ref, topic, event, payload]. Every later message to this topic
    // must repeat the same join_ref.
    const frame = [String(index), String(index), topic, "phx_join", joinPayload];
    ws.send(JSON.stringify(frame));
    // Echo the frame we just sent. It is the whole point of this script: what
    // CHANNELS.md documents is literally what goes out.
    console.log(`${stamp()}  ${label(topic)} -> ${JSON.stringify(frame)}`);
  });
  console.log();

  const timer = setInterval(
    () => ws.send(JSON.stringify([null, "hb", "phoenix", "heartbeat", {}])),
    HEARTBEAT_MS
  );
  timer.unref?.();
});

// `unmatched topic` is the server saying it has never heard of the topic, which
// on a correct client means the host predates it - or that the topic is
// misspelled, which looks identical. Both are worth naming in a tool whose whole
// job is trying one channel at a time.
const UNMATCHED_TOPIC_HINT = `
  'unmatched topic' means the server does not know that topic.
  Check the spelling against CHANNELS.md - and note that the dollar-format
  topics need a host running them, while older deployments carry only the
  legacy cents topics.
`;
let warnedUnmatched = false;

ws.on("message", (data) => {
  const [, , topic, event, payload] = JSON.parse(data);
  if (topic === "phoenix") return; // heartbeat acks, not channel traffic
  // A join snapshot can carry hundreds of rows, so payloads are clipped by
  // default. --full turns that off, which is what you want when reading one
  // message rather than watching a stream.
  const body = JSON.stringify(payload);
  const shown =
    maxChars !== null && body.length > maxChars
      ? `${body.slice(0, maxChars)}... [${body.length} chars, --full to see it all]`
      : body;
  console.log(`${stamp()}  ${label(topic)} <- ${event}  ${shown}`);

  // The raw frame above is the point of this script, so the reason is printed
  // beside it rather than instead of it. Once per run: on an older host every
  // dollar topic fails the same way.
  if (!warnedUnmatched && payload?.response?.reason === "unmatched topic") {
    console.error(UNMATCHED_TOPIC_HINT);
    warnedUnmatched = true;
  }
});

ws.on("error", (error) => fail(`socket error: ${error.message}`));
ws.on("close", (code) => console.log(`\nsocket closed (${code})`));

process.on("SIGINT", () => {
  ws.close();
  console.log("\nstopped");
  process.exit(0);
});
