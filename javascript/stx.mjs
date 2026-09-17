// Shared plumbing for the JavaScript examples: hosts, profiles, and signing.
//
// Three things live here because they must be identical everywhere and because
// a base URL should appear exactly once in this repository per language:
//
//   BASE_URLS      region + env -> host. The one table for JavaScript.
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
// A profile names a region and an environment, never a hostname, so this table
// is the only place one appears. Only the public environments are listed; US
// production is not open yet. Anything else goes in a `base_url` line.
//
// Markets settle at $1, so max_price is "1.0000" and quotes run $0.01-$0.99.
// Read max_price off the market rather than assuming it.
// ---------------------------------------------------------------------------

export const BASE_URLS = {
  "us/demo": "https://demo.stxapp.io",
  "ontario/demo": "https://demo.stxapp.ca",
  "ontario/prod": "https://stxapp.ca",
};

// A host not in that table - a local server, a review app - is set with
// STX_BASE_URL, or a `base_url` line in the profile. It wins over the table:
//
//   STX_BASE_URL=http://localhost:8000 STX_ENV=local node javascript/rest/quickstart.mjs markets
//
// `env` is still required alongside a base_url, because it decides more than
// the host: `roundtrip` and `latency.mjs` refuse to place orders when env is
// `prod`. Point base_url at a real exchange and that guard is all that stands
// between an example and a live book, so set env truthfully.

// Earlier versions of ./configure wrote `exchange`, `environment` and
// `private_key`. They are still read, and translated, so an existing
// credentials file keeps working.
const LEGACY_REGIONS = { ca: "ontario" };
const LEGACY_ENVS = { integration: "demo", production: "prod" };
const KNOWN_KEYS = ["region", "env", "key_id", "key_file", "base_url",
  "exchange", "environment", "private_key"];

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
 * STX_PROFILE, STX_REGION, STX_ENV, STX_KEY_ID, STX_KEY_FILE, STX_BASE_URL.
 */
export function loadProfile(name) {
  const profile = name || process.env.STX_PROFILE || "default";
  let values = {};
  let hasSection = false;

  if (existsSync(CREDENTIALS_PATH)) {
    const sections = parseIni(readFileSync(CREDENTIALS_PATH, "utf8"));
    if (sections[profile]) {
      hasSection = true;
      values = sections[profile];
    } else if (profile !== "default") {
      const available = Object.keys(sections).join(", ") || "none";
      fail(`Profile "${profile}" not found in ${CREDENTIALS_PATH}. Available: ${available}.
Run ./configure ${profile}`);
    }
  }

  // A misspelt key is otherwise silently ignored, and the profile quietly
  // resolves to something you did not ask for.
  const unknown = Object.keys(values).filter((k) => !KNOWN_KEYS.includes(k)).sort();
  if (unknown.length) {
    console.error(`warning: [${profile}] has unrecognised keys ${unknown.join(", ")}; ` +
      `expected region, env, key_id, key_file, base_url`);
  }

  const pick = (envVars, keys) => {
    for (const v of envVars) if (process.env[v]) return process.env[v];
    for (const k of keys) if (values[k]) return values[k];
    return undefined;
  };

  let region = pick(["STX_REGION", "STX_EXCHANGE"], ["region", "exchange"]);
  let env = pick(["STX_ENV", "STX_ENVIRONMENT"], ["env", "environment"]);
  region = LEGACY_REGIONS[region] || region;
  env = LEGACY_ENVS[env] || env;

  // A trailing slash would produce //api/v1, which some routers 404 on.
  let baseUrl = (pick(["STX_BASE_URL"], ["base_url"]) || "").replace(/\/+$/, "");

  if (baseUrl) {
    if (!env) {
      fail(`Profile [${profile}] sets base_url but no env. Add \`env = <name>\`
(\`prod\` if that host takes real money) so the order guard knows.`);
    }
  } else {
    if (!hasSection && !region && !env) {
      // No profile and no overrides: the documented zero-config path,
      // STX_KEY_ID and STX_KEY_FILE alone, against the US demo exchange.
      region = "us";
      env = "demo";
    }
    baseUrl = BASE_URLS[`${region}/${env}`];
    if (!baseUrl) {
      fail(`No host for region "${region ?? "(not set)"}" env "${env ?? "(not set)"}" in profile [${profile}].
Known: ${Object.keys(BASE_URLS).join(", ")}
For any other host add a base_url line.`);
    }
  }

  const keyId = pick(["STX_KEY_ID"], ["key_id"]);
  const keyFile = pick(["STX_KEY_FILE", "STX_PRIVATE_KEY"], ["key_file", "private_key"]);
  if (!keyId || !keyFile) {
    fail(`Profile [${profile}] has no key_id or key_file. Run ./configure ${profile}`);
  }

  return {
    profile,
    region,
    env,
    baseUrl,
    // socketUrl is the real handshake URL, for a raw WebSocket client.
    // socketEndpoint is what `phoenix` wants - see SOCKET_ENDPOINT above.
    socketUrl: wsBase(baseUrl) + SOCKET_PATH,
    socketEndpoint: wsBase(baseUrl) + SOCKET_ENDPOINT,
    keyId,
    privateKey: createPrivateKey(readFileSync(expandHome(keyFile))),
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
    `  to the host for this region/env pair.`
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
// width is a MINIMUM, not a promise: a computed field such as a fee can carry
// more.
//
// An order price is a whole number of cents: at most two decimal places, not
// counting trailing zeros. "0.49" and "0.4900" are accepted, "0.495" is a 400.
//
// Not every number is money. `price_change24h` is a percentage and `points` are
// loyalty points; both stay plain JSON numbers. Convert what is an amount of
// money or a count of contracts, nothing else.
//
// Going the other way, `price` and `quantity` on POST /api/v1/orders must both
// be strings. An integer price is rejected with a 400 rather than guessed at,
// because a legacy client's 5600 meant $56.00 and reading it as $5,600.00 would
// be a 100x overprice. `quantity` refuses numbers for a different reason: a
// float arrives as an IEEE-754 double, so a sent 2.675 would rest on the book
// as 2.67499999999999982... Integers are exact, but accepting them while
// refusing floats is harder to state than to follow, so every number is a 400.
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
 * A price as the dollar string the API takes for an order: 0.51 -> "0.5100".
 *
 * Four decimals, matching the width the server echoes back. The input side is
 * looser than the output - "0.51" and "0.5100" are the same order - so you
 * never have to match the server's width.
 *
 * A price must be a whole number of cents. float64 noise is rounded away, since
 * `0.24 - 0.1` is 0.13999999999999999 here and means 14 cents. A value that is
 * genuinely finer, such as 0.495, throws instead of being rounded, because
 * rounding would quietly place a different order, and sending it is a 400.
 */
export function dollarString(value) {
  const number = Number(value);
  const cents = Math.round(number * 100);
  if (!Number.isFinite(number) || Math.abs(number * 100 - cents) > 1e-6) {
    throw new Error(
      `price ${value} is not a whole number of cents; prices take at most two decimal places`
    );
  }
  return (cents / 100).toFixed(4);
}
