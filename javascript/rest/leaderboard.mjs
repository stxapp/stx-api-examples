// STX leaderboard - read one board, your own standing, and edit your public profile.
//
//   node javascript/rest/leaderboard.mjs board                        # weekly volume, all sports, top 10
//   node javascript/rest/leaderboard.mjs board --metric profit --period all
//   node javascript/rest/leaderboard.mjs board --category basketball --limit 25
//   node javascript/rest/leaderboard.mjs me                           # your rank on every board
//   node javascript/rest/leaderboard.mjs me --category basketball --period monthly
//   node javascript/rest/leaderboard.mjs profile                      # your handle, avatar, opt-in
//   node javascript/rest/leaderboard.mjs profile --handle swift.fox12
//   node javascript/rest/leaderboard.mjs profile --opt-in false       # leave the leaderboard
//   node javascript/rest/leaderboard.mjs profile --reroll-avatar
//
// Two leaderboard endpoints: GET /api/v1/leaderboard (one board) and
// GET /api/v1/leaderboard/me (your standing). `profile` uses GET /api/v1/me and
// PATCH /api/v1/me/profile.
//
// --category is `all` (every sport) or one sport key, the lowercase sport name as
// in a row's `top_sport`. Sport keys are the sports a market has traded or
// settled in during the period; any other key is simply an empty board.
//
// --metric and --limit are always sent. Leave `metric` off and the server picks
// the operator's opening board, which a client cannot look up. `limit` is
// clamped to the operator's players per board (at most 100). A board the
// operator hides answers with `shown: false` and no rows.
//
// Every row on a board is public identity only: a handle, an avatar URL and the
// ranked value. No account ids, names or balances are ever returned.
//
// Reads work with a read_only key. `profile` with a change needs read_write.
// On a deployment where the leaderboard is switched off, every route is a 404.
//
// Node 20 or newer; nothing to install beyond `./install.sh`.

import { loadProfile, signedHeaders, parseArgs, fail, fmtMoney, unreachable } from "../stx.mjs";

const PERIODS = ["daily", "weekly", "monthly", "yearly", "all"];
// How each board writes `value`: a dollar string, an integer, or a 0-1 ratio.
const MONEY = ["volume", "profit", "biggest_win"];
const COUNTS = ["predictions", "markets", "streak"];
const RATIOS = ["win_rate", "return"];
const METRICS = [...MONEY, ...COUNTS, ...RATIOS];
const AVATAR_STYLES = ["dots", "rings", "stripes", "grid", "ball", "court", "stitch", "target", "candles", "dice"];
const AVATAR_PALETTES = ["ember", "forest", "ocean", "grape", "slate", "mint", "rose", "gold"];

// One signed request. The signature covers the path INCLUDING the query string.
async function request(config, method, path, body) {
  let response;
  try {
    response = await fetch(config.baseUrl + path, {
      method,
      headers: { ...signedHeaders(config, method, path), "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (error) {
    fail(unreachable(config.baseUrl, error));
  }
  const text = await response.text();
  if (response.status === 404 && text.includes("not enabled")) fail("The leaderboard is not enabled on this deployment.");
  if (!response.ok) fail(`${method} ${path} -> HTTP ${response.status}: ${text.slice(0, 300)}`);
  return JSON.parse(text);
}

// Money boards are dollar strings, counts are integers, ratios are 0-1 numbers.
function fmtValue(metric, value) {
  if (value === null || value === undefined) return "-";
  if (COUNTS.includes(metric)) return Number(value).toLocaleString("en-US");
  if (RATIOS.includes(metric)) return `${(value * 100).toFixed(1)}%`;
  return fmtMoney(value);
}

// Top rows of one board. No cursor: a board is a fixed top list.
async function cmdBoard(config, args) {
  const query = new URLSearchParams({ period: args.period, category: args.category, metric: args.metric, limit: args.limit });
  const board = await request(config, "GET", `/api/v1/leaderboard?${query}`);
  console.log(`${board.metric} · ${board.period} · ${board.category}   (snapshot ${board.refreshed_at}, resets ${board.next_reset_at ?? "never"})`);
  if (!board.shown) console.log("  this board is hidden by the operator on this deployment");
  else if (board.leaderboard.length === 0) console.log("  nobody is ranked here yet");
  for (const row of board.leaderboard) {
    console.log(`  ${String(row.rank).padStart(3)}  ${row.handle.padEnd(24)} ${fmtValue(board.metric, row.value).padStart(14)}   ${(row.top_sport ?? "-").padEnd(12)} ${config.baseUrl}${row.avatar_url}`);
  }
}

// Your own standing on every board, including a rank outside the list.
async function cmdMe(config, args) {
  const query = new URLSearchParams({ period: args.period, category: args.category });
  const me = await request(config, "GET", `/api/v1/leaderboard/me?${query}`);
  console.log(`${me.period} · ${me.category}   listed: ${me.opted_in ? "yes" : "no"}`);
  for (const metric of METRICS) {
    // Win rate is the one board whose rank sits under its own key.
    const stat = me[metric === "win_rate" ? "win_rate_rank" : metric];
    console.log(`  ${metric.padEnd(12)} ${stat ? `#${stat.rank}  ${fmtValue(metric, stat.value)}` : "unranked"}`);
  }
  console.log(`  win rate     ${fmtValue("win_rate", me.win_rate)}   settled markets ${me.settled_markets}`);
}

async function cmdProfile(config, args) {
  const body = {};
  if (args.handle) body.handle = args.handle;
  if (args["opt-in"] !== undefined) body.leaderboard_opt_in = String(args["opt-in"]).toLowerCase() === "true";
  if (args["reroll-avatar"] !== undefined) {
    const pick = (list) => list[Math.floor(Math.random() * list.length)];
    body.avatar = {
      style: pick(AVATAR_STYLES),
      seed: Math.floor(Math.random() * 2 ** 32).toString(16).padStart(8, "0"),
      palette: pick(AVATAR_PALETTES),
    };
  }

  let me;
  if (Object.keys(body).length > 0) {
    // 422 carries the reason: taken, reserved, malformed, or changed within the
    // last 30 days. Sending your current handle again is not a change.
    ({ me } = await request(config, "PATCH", "/api/v1/me/profile", body));
    console.log("updated");
  } else {
    ({ me } = await request(config, "GET", "/api/v1/me"));
  }
  // A player who has never opened the leaderboard has no handle or avatar yet.
  console.log(`  handle        ${me.handle ?? "-"}`);
  console.log(`  avatar        ${me.avatar_url ? config.baseUrl + me.avatar_url : "-"}`);
  console.log(`  listed        ${me.leaderboard_opt_in ? "yes" : "no"}`);
  console.log(`  handle change ${me.handle_changeable_at === null ? "allowed now" : "after " + me.handle_changeable_at}`);
}

const COMMANDS = { board: cmdBoard, me: cmdMe, profile: cmdProfile };

const args = parseArgs();
const command = args._?.[0] ?? "board";
if (!COMMANDS[command]) fail(`unknown command ${command}; one of ${Object.keys(COMMANDS).join(", ")}`);
args.period = args.period ?? "weekly";
args.category = args.category ?? "all";
args.metric = args.metric ?? "volume";
args.limit = Number(args.limit ?? 10);
if (!PERIODS.includes(args.period)) fail(`--period must be one of ${PERIODS.join(", ")}`);
if (!METRICS.includes(args.metric)) fail(`--metric must be one of ${METRICS.join(", ")}`);
if (!Number.isInteger(args.limit) || args.limit < 1 || args.limit > 100) fail("--limit must be 1 to 100");

const config = loadProfile(args.profile);
console.error(`[${config.profile} -> ${config.baseUrl}]\n`);
await COMMANDS[command](config, args);
