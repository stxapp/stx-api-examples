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
// nothing here or in the REST and GraphQL examples needs npm at all. Only the
// WebSocket examples pull in packages.

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
// The US exchange settles markets at $1: prices run 1-99 cents against a
// max_price of 100. Canada settles at $100, so max_price is 10000 there. Read
// max_price off the market rather than assuming either.
// ---------------------------------------------------------------------------

export const BASE_URLS = {
  "us/integration": "https://demo.stxapp.io",
  "ca/integration": "https://api-staging.on.sportsxapp.com",
  "ca/production": "https://api.on.stxapp.ca",
};

// The handshake path, and the path the handshake signature covers.
export const SOCKET_PATH = "/socket/websocket";

// What the `phoenix` client wants: it appends "/websocket" to the endpoint you
// hand it. Give it SOCKET_PATH and you connect to /socket/websocket/websocket,
// which is a 404 at the handshake and shows up as an endless reconnect loop.
export const SOCKET_ENDPOINT = "/socket";
export const GRAPHQL_PATH = "/api/graphql";

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

function expandHome(path) {
  return path.startsWith("~/") ? join(homedir(), path.slice(2)) : path;
}

/**
 * Resolve one profile from ~/.stx/credentials.
 *
 * Environment variables win over the file, which is what you want in CI:
 * STX_PROFILE, STX_EXCHANGE, STX_ENVIRONMENT, STX_KEY_ID, STX_PRIVATE_KEY.
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
  const baseUrl = BASE_URLS[`${exchange}/${environment}`];

  if (!baseUrl) {
    fail(`No host for exchange "${exchange}" environment "${environment}".
Known: ${Object.keys(BASE_URLS).join(", ")}`);
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
    socketUrl: baseUrl.replace(/^https:/, "wss:") + SOCKET_PATH,
    socketEndpoint: baseUrl.replace(/^https:/, "wss:") + SOCKET_ENDPOINT,
    keyId,
    privateKey: createPrivateKey(readFileSync(expandHome(keyPath))),
  };
}

// ---------------------------------------------------------------------------
// Signing
//
// Three headers on every /api/v1 call. There are no public REST endpoints, so
// this runs on every request you will ever make.
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

/** Parse `--flag value` and `--flag=value` alike. Returns a plain object. */
export function parseArgs(argv = process.argv.slice(2)) {
  const args = { _: [] };
  for (let i = 0; i < argv.length; i++) {
    const token = argv[i];
    if (!token.startsWith("--")) {
      args._.push(token);
      continue;
    }
    const [name, inline] = token.slice(2).split(/=(.*)/s);
    if (inline !== undefined) {
      args[name] = inline;
    } else if (argv[i + 1] !== undefined && !argv[i + 1].startsWith("--")) {
      args[name] = argv[++i];
    } else {
      args[name] = true;
    }
  }
  return args;
}


// ---------------------------------------------------------------------------
// Prices
//
// The two halves of the API do not agree on units. This is the one place that
// reconciles them, so that no example has to remember which is which:
//
//   market.bids[0].price   "0.54"   decimal DOLLARS, sent as a string
//   socket book level.p    "0.54"   decimal DOLLARS
//   market.max_price       100      integer CENTS
//   order.price            54       integer CENTS
//
// Orders are placed and returned in cents, so any arithmetic against the touch
// has to convert first. `"0.54" - 10` does not throw here the way it does in
// Python - it quietly evaluates to -9.46 and prices your order at the floor.
// ---------------------------------------------------------------------------

/** A book or quote price as the integer cents that orders are priced in. */
export function bookPriceCents(price) {
  return Math.round(Number(price) * 100);
}
