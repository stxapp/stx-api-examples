// Shared plumbing for the JavaScript examples: hosts, profiles, and signing.
//
// Three things live here because they must be identical everywhere and because
// a base URL should appear exactly once in this repository per language:
//
//   BASE_URLS      exchange + environment -> host. The one table for JavaScript.
//   loadProfile()  reads ~/.stx/credentials, the file ./configure writes.
//   signedHeaders() the Ed25519 signing scheme, in about ten lines.
//
// Zero dependencies: Node has Ed25519 in node:crypto and fetch built in, so
// nothing here or in the REST examples needs npm at all. Only the WebSocket
// examples pull in packages.

import { createPrivateKey, sign } from "node:crypto";
import { readFileSync, existsSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

// ---------------------------------------------------------------------------
// Hosts
//
// `configure` stores an exchange and an environment, never a hostname, so this
// table is the only place one appears.
//
// The US exchange settles markets at $1, so max_price is "1.0000" and quotes
// run $0.01-$0.99. Canada settles at $100, so max_price is "100.0000" there.
// Read max_price off the market rather than assuming either.
// ---------------------------------------------------------------------------

export const BASE_URLS = {
  "us/integration": "https://demo.stxapp.io",
  "ca/integration": "https://api-staging.on.sportsxapp.com",
  "ca/production": "https://api.on.stxapp.ca",
};

// A host not in that table - a local server, a review app - is set with
// STX_BASE_URL, or a `base_url` line in the profile. It wins over the pair:
//
//   STX_BASE_URL=http://localhost:8000 node javascript/rest/quickstart.mjs markets
//
// `exchange` and `environment` still apply, because they decide more than the
// host: `roundtrip` and `latency.mjs` refuse to place orders when environment is
// `production`. Point base_url at a real exchange and that guard is all that
// stands between an example and a live book, so leave environment alone unless
// you mean it.

// The handshake path, and the path the handshake signature covers.
export const SOCKET_PATH = "/socket/websocket";

// What the `phoenix` client wants: it appends "/websocket" to the endpoint you
// hand it. Give it SOCKET_PATH and you connect to /socket/websocket/websocket,
// which is a 404 at the handshake and shows up as an endless reconnect loop.
export const SOCKET_ENDPOINT = "/socket";

const CREDENTIALS_PATH =
  process.env.STX_CREDENTIALS || join(homedir(), ".stx", "credentials");

// ---------------------------------------------------------------------------
// Profiles
// ---------------------------------------------------------------------------

function parseIni(text) {
  const sections = {};
  let current = null;
  for (const rawLine of text.split("\n")) {
    const line = rawLine.replace(/[;#].*$/, "").trim();
    if (!line) continue;
    const header = line.match(/^\[(.+)\]$/);
    if (header) {
      current = header[1];
      sections[current] = {};
      continue;
    }
    const separator = line.indexOf("=");
    if (separator === -1 || current === null) continue;
    sections[current][line.slice(0, separator).trim()] = line.slice(separator + 1).trim();
  }
  return sections;
}

/**
 * The WebSocket origin for an API base URL.
 *
 * http maps to ws as well as https to wss, so a local server on plain http
 * works. Mapping only https would leave the scheme untouched and the socket
 * would fail to connect with no useful message.
 */
function wsBase(baseUrl) {
  if (baseUrl.startsWith("https://")) return `wss://${baseUrl.slice(8)}`;
  if (baseUrl.startsWith("http://")) return `ws://${baseUrl.slice(7)}`;
  fail(`base_url must start with http:// or https://, got "${baseUrl}"`);
}

function expandHome(path) {
  return path.startsWith("~/") ? join(homedir(), path.slice(2)) : path;
}

/**
 * Resolve one profile from ~/.stx/credentials.
 *
 * Environment variables win over the file, which is what you want in CI:
 * STX_PROFILE, STX_EXCHANGE, STX_ENVIRONMENT, STX_KEY_ID, STX_PRIVATE_KEY,
 * STX_BASE_URL.
 */
export function loadProfile(name) {
  const profile = name || process.env.STX_PROFILE || "default";
  let values = {};

  if (existsSync(CREDENTIALS_PATH)) {
    const sections = parseIni(readFileSync(CREDENTIALS_PATH, "utf8"));
    if (sections[profile]) {
      values = sections[profile];
    } else if (profile !== "default") {
      const available = Object.keys(sections).join(", ") || "none";
      fail(`Profile "${profile}" not found in ${CREDENTIALS_PATH}. Available: ${available}.
Run ./configure ${profile}`);
    }
  }

  const pick = (envVar, key, fallback) => process.env[envVar] || values[key] || fallback;

  const exchange = pick("STX_EXCHANGE", "exchange", "us");
  const environment = pick("STX_ENVIRONMENT", "environment", "integration");

  // A trailing slash would produce //api/v1, which some routers 404 on.
  const override = (pick("STX_BASE_URL", "base_url") || "").replace(/\/+$/, "");
  const baseUrl = override || BASE_URLS[`${exchange}/${environment}`];

  if (!baseUrl) {
    fail(`No host for exchange "${exchange}" environment "${environment}".
Known: ${Object.keys(BASE_URLS).join(", ")}
Set STX_BASE_URL to use a host that is not in that list.`);
  }

  const keyId = pick("STX_KEY_ID", "key_id");
  const keyPath = pick("STX_PRIVATE_KEY", "private_key");
  if (!keyId || !keyPath) {
    fail(`Profile [${profile}] has no key_id or private_key. Run ./configure ${profile}`);
  }

  return {
    profile,
    exchange,
    environment,
    baseUrl,
    // socketUrl is the real handshake URL, for a raw WebSocket client.
    // socketEndpoint is what `phoenix` wants - see SOCKET_ENDPOINT above.
    socketUrl: wsBase(baseUrl) + SOCKET_PATH,
    socketEndpoint: wsBase(baseUrl) + SOCKET_ENDPOINT,
    keyId,
    privateKey: createPrivateKey(readFileSync(expandHome(keyPath))),
  };
}

// ---------------------------------------------------------------------------
// Signing
//
// Three headers on every /api/v1 call. Every route needs them, so this runs on
// every request you will ever make.
//
//   X-STX-ACCESS-KEY         your key id
//   X-STX-ACCESS-TIMESTAMP   Unix milliseconds, as a string
//   X-STX-ACCESS-SIGNATURE   base64 Ed25519 signature of the message below
//
// The message is a bare concatenation, with no separators:
//
//   timestamp_ms + HTTP_METHOD_UPPERCASE + path
//
// The body is NOT signed. The path carries its query string when there is one -
// /api/v1/markets?status=open signs with the query attached - but never the
// scheme or host. Plain Ed25519 (RFC 8032) over the UTF-8 bytes, not the
// Ed25519ph pre-hashed variant, base64 with the standard alphabet and padding.
//
// The null first argument to crypto.sign is not an oversight: Ed25519 does its
// own hashing internally, and Node requires the digest be left unspecified.
//
// The timestamp must be within 30 seconds of the server clock, so generate it
// per request and keep the machine on NTP. A clock 40 seconds fast fails every
// request with a 401 that looks exactly like a bad key.
//
// The WebSocket handshake signs the same way, with one difference: the path is
// /socket/websocket with any query string DROPPED, and the method is GET.
// ---------------------------------------------------------------------------

/**
 * The message for a request that never reached the host.
 *
 * Connection refused is the ordinary first result of pointing STX_BASE_URL at a
 * server that is not running, so it gets a sentence rather than a stack trace.
 */
export function unreachable(baseUrl, error) {
  // Node wraps a connection failure as TypeError("fetch failed") whose `cause`
  // is an AggregateError with an EMPTY message and one entry per address it
  // tried - localhost resolves to both 127.0.0.1 and ::1. Reading `.message`
  // alone prints a blank line, so fall through to the individual errors.
  const cause = error?.cause;
  const reason =
    cause?.errors?.map((e) => e.message).join("; ") ||
    cause?.message ||
    cause?.code ||
    error?.message ||
    String(error);

  return (
    `Cannot reach ${baseUrl}\n` +
    `  ${reason}\n` +
    `  If that is a local server, check it is running and on that port.\n` +
    `  Unset STX_BASE_URL (or drop base_url from your profile) to go back\n` +
    `  to the host for this exchange/environment pair.`
  );
}

export function signedHeaders(config, method, path) {
  const timestamp = String(Date.now());
  const message = `${timestamp}${method.toUpperCase()}${path}`;
  const signature = sign(null, Buffer.from(message, "utf8"), config.privateKey);
  return {
    "X-STX-ACCESS-KEY": config.keyId,
    "X-STX-ACCESS-TIMESTAMP": timestamp,
    "X-STX-ACCESS-SIGNATURE": signature.toString("base64"),
  };
}

// ---------------------------------------------------------------------------
// Small shared conveniences
// ---------------------------------------------------------------------------

export function fail(message) {
  console.error(message);
  process.exit(1);
}

/**
 * Parse `--flag value` and `--flag=value` alike. Returns a plain object.
 *
 * A flag given once is a string; repeating it collects an array, so
 * `--topic a --topic b` yields ["a", "b"] rather than silently keeping the
 * last. Use `argList()` when you want the array shape either way.
 */
export function parseArgs(argv = process.argv.slice(2)) {
  const args = { _: [] };
  const set = (name, value) => {
    if (!(name in args)) args[name] = value;
    else if (Array.isArray(args[name])) args[name].push(value);
    else args[name] = [args[name], value];
  };
  for (let i = 0; i < argv.length; i++) {
    const token = argv[i];
    if (!token.startsWith("--")) {
      args._.push(token);
      continue;
    }
    const [name, inline] = token.slice(2).split(/=(.*)/s);
    if (inline !== undefined) {
      set(name, inline);
    } else if (argv[i + 1] !== undefined && !argv[i + 1].startsWith("--")) {
      set(name, argv[++i]);
    } else {
      set(name, true);
    }
  }
  return args;
}

/** One parseArgs value as an array: missing -> [], single -> [value]. */
export function argList(value) {
  if (value === undefined) return [];
  return Array.isArray(value) ? value : [value];
}


// ---------------------------------------------------------------------------
// Money and quantities
//
// Every money and quantity field on /api/v1 is a fixed-point DECIMAL STRING, in
// dollars. Not cents, not a JSON number:
//
//   market.max_price             "1.0000"    $1, a US market's ceiling
//   market.bids[0].price         "0.6100"    $0.61
//   market.bids[0].quantity      "491.00"    contracts
//   order.price                  "0.5100"    $0.51, or null on a market order
//   order.quantity, order.filled "1.00"      contracts
//
// Money carries at least four decimals and quantities at least two, but the
// width is a MINIMUM, not a promise: an order price can carry seven.
//
// Not every number is money. `price_change24h` is a percentage and `points` are
// loyalty points; both stay plain JSON numbers. Convert what is an amount of
// money or a count of contracts, nothing else.
//
// Going the other way, `price` on POST /api/v1/orders must be a string. An
// integer is rejected with a 400 rather than guessed at, because a legacy
// client's 5600 meant $56.00 and reading it as $5,600.00 would be a 100x
// overprice. `quantity` still accepts a number, since a contract count has no
// unit ambiguity.
//
// JavaScript has no decimal type, so these strings become float64. That is
// exact enough for the two-decimal quotes these markets trade at and for the
// arithmetic in these examples, but it is not a money type: 0.1 + 0.2 is
// 0.30000000000000004 here. Anything that accumulates - a running P&L, a
// position cost basis - wants a decimal library instead.
//
// Do not skip the parse and lean on coercion. `"0.61" - 0.1` happens to give
// 0.51, but `"0.61" + 0.1` is the string "0.610.1", and nothing warns you.
// ---------------------------------------------------------------------------

/** One money or quantity field as a number. null and undefined pass through. */
export function toNumber(value) {
  return value === null || value === undefined ? value : Number(value);
}

/**
 * One money field as a display string: "0.6100" -> "$0.61".
 *
 * Display only. Never build a request body from this - the wire wants
 * `dollarString`, and a value rounded for a column is not the value.
 */
export function fmtMoney(value, places = 2) {
  return value === null || value === undefined ? "-" : `$${Number(value).toFixed(places)}`;
}

/**
 * A number as the dollar string the API takes for an order price.
 *
 * At least four decimals, matching the width the server echoes back, but never
 * fewer than the value carries: a price may hold up to seven, and rounding one
 * off here would quietly place a different order.
 *
 * The input side is looser than the output - "0.51", "0.5100" and "0.510000"
 * are the same order - so you never have to match the server's width.
 */
export function dollarString(value) {
  const number = Number(value);
  const carried = (String(number).split(".")[1] ?? "").length;
  return number.toFixed(Math.max(4, carried));
}

// ---------------------------------------------------------------------------
// The legacy socket topics
//
// The pre-SX-12037 WebSocket topics were not converted and still send integer
// cents, and one `market:` join reply carries the book twice in two units (`ob`
// in dollars, `bids`/`offers` in cents). None of the examples here join them any
// more - they use the dollar topics, which agree with /api/v1 field for field.
// CHANNELS.md documents both and how they map onto each other.
// ---------------------------------------------------------------------------
