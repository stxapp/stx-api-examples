// Join one channel and print every frame it sends.
//
//   node javascript/websockets/watch_channel.mjs --topic markets
//   node javascript/websockets/watch_channel.mjs --topic 'active_orders:<user_id>'
//   node javascript/websockets/watch_channel.mjs --topic 'market:<market_id or symbol>'
//
// --payload sets the join payload, which the markets channel uses for
// server-side filtering:
//
//   node javascript/websockets/watch_channel.mjs --topic markets \
//     --payload '{"rule_filters": ["home_winner"]}'
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
import { loadProfile, signedHeaders, parseArgs, argList, fail, SOCKET_PATH } from "../stx.mjs";

// The socket closes after 60s of silence. This is the socket keep-alive, not
// the cancel_on_disconnect ping, which is a separate timer on active_orders;
// see CHANNELS.md. watch.mjs is the example that negotiates that one.
const HEARTBEAT_MS = 20_000;

const stamp = () => new Date().toISOString().slice(11, 19);

// The topic's prefix, padded. The full topic is on the join frame; repeating a
// user id on every line only makes them unreadable.
const label = (topic) => topic.split(":")[0].padEnd(18);

const args = parseArgs();
const config = loadProfile(args.profile);
const topics = argList(args.topic);
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
  const response = await fetch(config.baseUrl + path, {
    headers: signedHeaders(config, "GET", path),
  });
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

ws.on("message", (data) => {
  const [, , topic, event, payload] = JSON.parse(data);
  if (topic === "phoenix") return; // heartbeat acks, not channel traffic
  console.log(`${stamp()}  ${label(topic)} <- ${event}  ${JSON.stringify(payload).slice(0, 400)}`);
});

ws.on("error", (error) => fail(`socket error: ${error.message}`));
ws.on("close", (code) => console.log(`\nsocket closed (${code})`));

process.on("SIGINT", () => {
  ws.close();
  console.log("\nstopped");
  process.exit(0);
});
