/**
 * STX crypto markets with the TypeScript SDK: list the windows, read a book, quote both sides.
 *
 *     npx tsx crypto.ts windows                  # the open window and the next ones
 *     npx tsx crypto.ts windows --coin eth
 *     npx tsx crypto.ts book --coin btc --side up
 *     npx tsx crypto.ts quote --coin btc --side up --bid 0.45 --offer 0.55
 *     npx tsx crypto.ts quote --coin btc --side up --bid 0.45 --offer 0.55 --keep
 *     npx tsx crypto.ts results --coin btc       # how recent windows settled
 *
 * Each crypto market is one short window, 15 minutes today, on one coin. A
 * window has two markets: "Up" settles at $1 if the coin's price at the end of
 * the window is at or above its price at the start, "Down" settles at $1 if it
 * is not. A new window opens as the last one closes, so look the markets up
 * again each window instead of holding on to a market id.
 *
 * The SDK reads the same ~/.stx/credentials that ./configure writes. Pick a
 * profile with STX_PROFILE, for example `STX_PROFILE=us-demo`.
 *
 * `quote` places a limit buy at --bid and a limit sell at --offer on the
 * current window, shows them on the book, and cancels both unless --keep is
 * given. A sell needs no position: it opens a short. It needs a read_write key
 * and refuses any exchange but a demo one unless --force-production is given.
 *
 * Install: npm ci (in sdk/typescript)
 */

import { parseArgs } from "node:util";
import { randomUUID } from "node:crypto";

import { STX, STXRejectedException, STXValidationException } from "@stxapp/stx-typescript";
import type { Market } from "@stxapp/stx-typescript";

const COINS = ["btc", "eth"];
const SIDES = ["up", "down"];

/** "btc" or "eth": the market's `specifier`. */
const coinOf = (m: Market): string => (m.specifier ?? "").toLowerCase();

/** "up" or "down", from the market's `rules` ("crypto_up" / "crypto_down"). */
const sideOf = (m: Market): string => (m.rules ?? "").replace(/^crypto_/, "");

/**
 * [start, end] of the market's window. `event_start` is the start. The length
 * is the MIN<n> at the end of the symbol: STXCRYPTO-26OCT162330-UPBTCMIN15 is a
 * 15-minute window.
 */
function windowOf(m: Market): [Date, Date] {
  const start = new Date(m.event_start ?? 0);
  const minutes = Number(/MIN(\d+)$/.exec(m.symbol ?? "")?.[1] ?? 15);
  return [start, new Date(start.getTime() + minutes * 60_000)];
}

const fmtTime = (d: Date): string => d.toLocaleTimeString([], { hourCycle: "h23", timeZoneName: "short" });

function fmtLeft(end: Date): string {
  const seconds = Math.floor((end.getTime() - Date.now()) / 1000);
  return seconds > 0 ? `${Math.floor(seconds / 60)}m${String(seconds % 60).padStart(2, "0")}s` : "ended";
}

/** The first level's price. Pass a side from sortedBook, not a raw list. */
const best = (levels?: { price?: string | null }[] | null): string => levels?.[0]?.price ?? "-";

/** Prices are dollar strings. Compare them in whole cents, never as floats. */
const cents = (price: string): number => Math.round(Number(price) * 100);

type Level = NonNullable<Market["bids"]>[number];

/**
 * [bids, offers], each best first. Sort them yourself: the market lists bids
 * AND offers highest price first, so the raw offers[0] is the worst offer.
 */
function sortedBook(m: Market): [Level[], Level[]] {
  const price = (l: Level): number => cents(l.price ?? "0");
  return [
    [...(m.bids ?? [])].sort((a, b) => price(b) - price(a)),
    [...(m.offers ?? [])].sort((a, b) => price(a) - price(b)),
  ];
}

/**
 * False for a window whose end has passed. A demo exchange can still list an
 * old window as open or scheduled after it ended, with no book.
 */
const notEnded = (m: Market): boolean => windowOf(m)[1].getTime() > Date.now();

async function cryptoMarkets(client: STX, status: string[]): Promise<Market[]> {
  // Crypto markets are listed under the sport "Crypto".
  const markets: Market[] = [];
  for await (const m of client.iterMarkets({ sports: "Crypto", status })) {
    if (notEnded(m)) markets.push(m);
  }
  return markets;
}

async function currentMarket(client: STX, coin: string, side: string): Promise<Market> {
  const found = (await cryptoMarkets(client, ["open"]))
    .filter((m) => coinOf(m) === coin && sideOf(m) === side)
    .sort((a, b) => (a.event_start ?? "").localeCompare(b.event_start ?? ""));
  if (!found.length) fail(`No open ${coin.toUpperCase()} ${side} window right now. \`windows\` shows the next one.`);
  return found[0];
}

function fail(message: string): never {
  console.error(message);
  process.exit(1);
}

type Args = { coin?: string; side: string; bid: string; offer: string; quantity: string; keep: boolean; limit: number };

async function windows(client: STX, args: Args): Promise<number> {
  const markets = (await cryptoMarkets(client, ["open", "scheduled"]))
    .filter((m) => !args.coin || coinOf(m) === args.coin)
    .sort((a, b) =>
      (a.event_start ?? "").localeCompare(b.event_start ?? "") ||
      coinOf(a).localeCompare(coinOf(b)) ||
      (sideOf(a) === "up" ? -1 : 1));
  if (!markets.length) fail("No open or scheduled crypto markets.");

  const width = Math.max(...markets.map((m) => (m.symbol ?? "").length));
  console.log(`${"SYMBOL".padEnd(width)} ${"STATUS".padEnd(9)} ${"START".padStart(12)} ${"END".padStart(12)} ` +
    `${"LEFT".padStart(7)} ${"BID".padStart(7)} ${"OFFER".padStart(7)}`);
  for (const m of markets) {
    const [start, end] = windowOf(m);
    const left = m.status === "open" ? fmtLeft(end) : "";
    const [bids, offers] = sortedBook(m);
    console.log(`${(m.symbol ?? "").padEnd(width)} ${(m.status ?? "").padEnd(9)} ${fmtTime(start).padStart(12)} ` +
      `${fmtTime(end).padStart(12)} ${left.padStart(7)} ${best(bids).padStart(7)} ${best(offers).padStart(7)}`);
  }
  const openNow = markets.find((m) => m.status === "open");
  if (openNow) console.log(`\n${openNow.question}\n${openNow.description}`);
  return 0;
}

async function book(client: STX, args: Args): Promise<number> {
  // For a book that updates as it changes, subscribe to the market over the
  // SDK's WebSocket client instead.
  const market = await currentMarket(client, args.coin!, args.side);
  const [, end] = windowOf(market);
  console.log(`${market.symbol}  closes ${fmtTime(end)} (${fmtLeft(end)} left)`);
  console.log(`${market.question}\n`);

  const [bids, offers] = sortedBook(market);
  if (!bids.length && !offers.length) {
    console.log("The book is empty.");
    return 0;
  }
  console.log(`${"BID QTY".padStart(10)} ${"BID".padStart(7)}   ${"OFFER".padEnd(7)} OFFER QTY`);
  for (let i = 0; i < Math.max(bids.length, offers.length); i++) {
    const bid = bids[i];
    const offer = offers[i];
    console.log(`${(bid?.quantity ?? "").padStart(10)} ${(bid?.price ?? "").padStart(7)}   ` +
      `${(offer?.price ?? "").padEnd(7)} ${offer?.quantity ?? ""}`);
  }
  return 0;
}

/**
 * Read the market until both prices show. A new order answers `accepted` and
 * reaches the book a moment later, so the first read can miss it.
 */
async function waitOnBook(client: STX, marketId: string, bid: string, offer: string): Promise<Market | undefined> {
  for (let i = 0; i < 10; i++) {
    const m = await client.market(marketId);
    const bids = (m.bids ?? []).map((l) => cents(l.price ?? "0"));
    const offers = (m.offers ?? []).map((l) => cents(l.price ?? "0"));
    if (bids.includes(cents(bid)) && offers.includes(cents(offer))) return m;
    await new Promise((resolve) => setTimeout(resolve, 300));
  }
  return undefined;
}

async function quote(client: STX, args: Args): Promise<number> {
  const market = await currentMarket(client, args.coin!, args.side);
  // Prices run from one cent to one cent under max_price.
  if (!(cents(args.bid) >= 1 && cents(args.bid) < cents(args.offer) && cents(args.offer) < cents(market.max_price ?? "1"))) {
    fail(`Need 0.01 <= bid < offer < ${market.max_price}.`);
  }
  const [, end] = windowOf(market);
  console.log(`${market.symbol}  closes ${fmtTime(end)} (${fmtLeft(end)} left)`);
  const [bids, offers] = sortedBook(market);
  console.log(`book       best bid ${best(bids)}  best offer ${best(offers)}`);

  const placed: string[] = [];
  let exitCode = 0;
  try {
    for (const [action, price] of [["buy", args.bid], ["sell", args.offer]] as const) {
      // Price and quantity are dollar strings. The clientOrderId is your own id,
      // echoed on the order and on every update about it.
      const order = await client.placeOrder(market.market_id!, action, "limit", {
        price,
        quantity: args.quantity,
        clientOrderId: `crypto-${randomUUID()}`,
      });
      placed.push(order.id!);
      console.log(`placed     ${action.toUpperCase().padEnd(4)} ${args.quantity} @ ${price}  ${order.id}  status=${order.status}`);
    }

    const seen = await waitOnBook(client, market.market_id!, args.bid, args.offer);
    if (seen) {
      const [seenBids, seenOffers] = sortedBook(seen);
      console.log(`on book    best bid ${best(seenBids)}  best offer ${best(seenOffers)}`);
    } else {
      // Crossing your own resting order is rejected with
      // rejection_reason "match_with_self".
      for (const id of placed) {
        const o = await client.order(id);
        console.log(`not seen   ${o.action} @ ${o.price}  status=${o.status}  ${o.rejection_reason ?? ""}`);
      }
      exitCode = 1;
    }
  } catch (err) {
    if (!(err instanceof STXRejectedException || err instanceof STXValidationException)) throw err;
    console.error(`Order refused (${err.statusCode}): ${err.message}`);
    exitCode = 1;
  } finally {
    if (args.keep && exitCode === 0) {
      console.log("\nLeft on the book. Cancel each with client.cancelOrder(orderId).");
    } else {
      for (const id of placed) console.log(`cancelled  ${id}  status=${(await client.cancelOrder(id)).status}`);
    }
  }
  return exitCode;
}

async function results(client: STX, args: Args): Promise<number> {
  // Newest windows first. In each window one market is "won" and the other
  // "lost"; `result` is "pending" until then.
  const perWindow = args.coin ? 2 : 2 * COINS.length;
  const page = await client.markets({
    sports: "Crypto",
    status: ["closed", "resulted"],
    sortBy: "event_start",
    sortDirection: "desc",
    limit: args.limit * 2 * COINS.length,
  });
  const markets = page.items.filter((m) => !args.coin || coinOf(m) === args.coin).slice(0, args.limit * perWindow);
  if (!markets.length) fail("No settled crypto windows yet.");

  const width = Math.max(...markets.map((m) => (m.symbol ?? "").length));
  console.log(`${"SYMBOL".padEnd(width)} ${"STATUS".padEnd(9)} ${"END".padStart(12)} ${"RESULT".padEnd(8)} VOLUME`);
  for (const m of markets) {
    const [, end] = windowOf(m);
    console.log(`${(m.symbol ?? "").padEnd(width)} ${(m.status ?? "").padEnd(9)} ${fmtTime(end).padStart(12)} ` +
      `${(m.result ?? "").padEnd(8)} ${m.total_volume ?? "0.00"}`);
  }
  return 0;
}

const COMMANDS: Record<string, (client: STX, args: Args) => Promise<number>> = { windows, book, quote, results };

async function main(): Promise<number> {
  const { values, positionals } = parseArgs({
    allowPositionals: true,
    options: {
      coin: { type: "string" },
      side: { type: "string", default: "up" },
      bid: { type: "string", default: "0.45" },
      offer: { type: "string", default: "0.55" },
      quantity: { type: "string", default: "1" },
      keep: { type: "boolean", default: false },
      limit: { type: "string", default: "4" },
      "force-production": { type: "boolean", default: false },
    },
  });
  const command = positionals[0] ?? "windows";
  if (!COMMANDS[command]) fail(`Unknown command ${command}. Use one of: ${Object.keys(COMMANDS).join(", ")}.`);
  if (values.coin && !COINS.includes(values.coin)) fail(`--coin is one of: ${COINS.join(", ")}.`);
  if (!SIDES.includes(values.side!)) fail(`--side is one of: ${SIDES.join(", ")}.`);
  const args: Args = {
    coin: values.coin ?? (command === "book" || command === "quote" ? "btc" : undefined),
    side: values.side!,
    bid: values.bid!,
    offer: values.offer!,
    quantity: values.quantity!,
    keep: values.keep!,
    limit: Number(values.limit),
  };

  const client = new STX();
  // Orders go only to a demo exchange or your own machine unless you say otherwise.
  const host = new URL(client.baseUrl).hostname;
  if (command === "quote" && !values["force-production"] &&
      !(host.startsWith("demo.") || host === "localhost" || host === "127.0.0.1")) {
    fail(`Refusing to place orders on ${client.baseUrl} from an example script.\nPass --force-production if you really mean to.`);
  }
  console.error(`[${client.baseUrl}]\n`);
  return COMMANDS[command](client, args);
}

process.exitCode = await main();
