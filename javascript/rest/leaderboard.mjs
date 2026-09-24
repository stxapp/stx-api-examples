// STX leaderboard - read the public boards, your own standing, and edit your public profile.
//
//   node javascript/rest/leaderboard.mjs board                        # weekly profit, all categories
//   node javascript/rest/leaderboard.mjs board --metric volume --period all
//   node javascript/rest/leaderboard.mjs board --category basketball --limit 10
//   node javascript/rest/leaderboard.mjs categories                   # sports with activity
//   node javascript/rest/leaderboard.mjs me                           # your ranks and win rate
//   node javascript/rest/leaderboard.mjs profile                      # your handle, avatar, opt-in
//   node javascript/rest/leaderboard.mjs profile --handle swift.fox12
//   node javascript/rest/leaderboard.mjs profile --opt-in false       # leave the leaderboard
//   node javascript/rest/leaderboard.mjs profile --reroll-avatar
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
const METRICS = ["profit", "volume", "predictions"];
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

// Profit and volume are dollar strings; predictions is a plain count.
function fmtValue(metric, value) {
  if (value === null || value === undefined) return "-";
  return metric === "predictions" ? Number(value).toLocaleString("en-US") : fmtMoney(value, 0);
}

async function cmdBoard(config, args) {
  const path = `/api/v1/leaderboard?period=${args.period}&category=${args.category}&metric=${args.metric}&limit=${args.limit}`;
  const board = await request(config, "GET", path);
  console.log(`${args.metric} · ${args.period} · ${args.category}   (snapshot ${board.refreshed_at}, resets ${board.next_reset_at ?? "never"})`);
  if (board.leaderboard.length === 0) console.log("  nobody is ranked here yet");
  for (const row of board.leaderboard) {
    console.log(`  ${String(row.rank).padStart(3)}  ${row.handle.padEnd(24)} ${fmtValue(args.metric, row.value).padStart(14)}   ${config.baseUrl}${row.avatar_url}`);
  }
}

async function cmdCategories(config) {
  const { categories } = await request(config, "GET", "/api/v1/leaderboard/categories");
  for (const c of categories) console.log(`  ${c.key.padEnd(16)} ${c.label}`);
}

async function cmdMe(config, args) {
  const me = await request(config, "GET", `/api/v1/leaderboard/me?period=${args.period}&category=${args.category}`);
  console.log(`${args.period} · ${args.category}   listed: ${me.opted_in ? "yes" : "no"}`);
  for (const metric of METRICS) {
    const stat = me[metric];
    console.log(`  ${metric.padEnd(12)} ${stat ? `#${stat.rank}  ${fmtValue(metric, stat.value)}` : "unranked"}`);
  }
  const winRate = me.win_rate === null ? "-" : `${Math.round(me.win_rate * 100)}%`;
  console.log(`  win rate     ${winRate}   settled markets ${me.settled_markets}`);
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
  console.log(`  handle        ${me.handle}`);
  console.log(`  avatar        ${config.baseUrl}${me.avatar_url}`);
  console.log(`  listed        ${me.leaderboard_opt_in ? "yes" : "no"}`);
  console.log(`  handle change ${me.handle_changeable_at === null ? "allowed now" : "after " + me.handle_changeable_at}`);
}

const COMMANDS = { board: cmdBoard, categories: cmdCategories, me: cmdMe, profile: cmdProfile };

const args = parseArgs();
const command = args._?.[0] ?? "board";
if (!COMMANDS[command]) fail(`unknown command ${command}; one of ${Object.keys(COMMANDS).join(", ")}`);
args.period = args.period ?? "weekly";
args.category = args.category ?? "all";
args.metric = args.metric ?? "profit";
args.limit = Number(args.limit ?? 25);
if (!PERIODS.includes(args.period)) fail(`--period must be one of ${PERIODS.join(", ")}`);
if (!METRICS.includes(args.metric)) fail(`--metric must be one of ${METRICS.join(", ")}`);

const config = loadProfile(args.profile);
console.error(`[${config.profile} -> ${config.baseUrl}]\n`);
await COMMANDS[command](config, args);
