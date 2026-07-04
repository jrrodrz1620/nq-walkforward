// Tests for the TradingView export parser, mirroring ../test_dataio.py:
// old single-row format, old two-row Entry/Exit format, and the new 2024+
// format with net PnL repeated on both rows. Run with `npm test`.
import { test } from "node:test";
import assert from "node:assert/strict";
import { parseTV, groupByDay } from "../src/parse.js";

function rows(csv) {
  const lines = csv.trim().split("\n");
  const headers = lines[0].split(",");
  return lines.slice(1).map(l => {
    const v = l.split(",");
    const o = {};
    headers.forEach((h, i) => { o[h] = v[i] ?? ""; });
    return o;
  });
}

test("new format collapses entry/exit pairs (PnL repeated on both rows)", () => {
  const trades = parseTV(rows(
    "Trade number,Type,Date and time,Signal,Price USD,Net PnL USD,Cumulative PnL USD\n" +
    "1,Exit long,2025-06-30 03:10,Short,22860.25,42.5,42.5\n" +
    "1,Entry long,2025-06-29 22:10,Long,22839,42.5,42.5\n" +
    "2,Exit short,2025-06-30 05:30,Long,22903.25,-86,-43.5\n" +
    "2,Entry short,2025-06-30 03:10,Short,22860.25,-86,-43.5\n"
  ));
  assert.equal(trades.length, 2);
  assert.equal(Math.round(trades.reduce((s, t) => s + t.profit, 0) * 100) / 100, -43.5);
  // Dated by the exit row: both trades realize P&L on 2025-06-30.
  assert.ok(trades.every(t => t.date === "2025-06-30"));
});

test("old two-row format takes profit from the exit row", () => {
  const trades = parseTV(rows(
    "Trade #,Type,Date/Time,Signal,Price,Profit\n" +
    "1,Exit long,2025-01-02 10:00,Sell,101,50\n" +
    "1,Entry long,2025-01-02 09:00,Buy,100,\n"
  ));
  assert.equal(trades.length, 1);
  assert.equal(trades[0].profit, 50);
  assert.equal(trades[0].date, "2025-01-02");
});

test("single-row-per-trade export passes through", () => {
  const trades = parseTV(rows(
    "Trade #,Type,Signal,Date/Time,Price,Profit\n" +
    "1,Entry long,Long,2025-01-02 09:00,100,50\n" +
    "2,Entry short,Short,2025-01-03 09:00,110,-20\n" +
    "3,Entry long,Long,2025-01-04 09:00,105,30\n"
  ));
  assert.equal(trades.length, 3);
  assert.equal(trades.reduce((s, t) => s + t.profit, 0), 60);
});

test("zero-PnL entry marker rows are skipped in single-row exports", () => {
  const trades = parseTV(rows(
    "Type,Date/Time,Profit\n" +
    "Entry long,2025-01-02 09:00,0\n" +
    "Exit long,2025-01-02 10:00,75\n"
  ));
  assert.equal(trades.length, 1);
  assert.equal(trades[0].profit, 75);
});

test("US-style dates normalize and group by day", () => {
  const daily = groupByDay(parseTV(rows(
    "Type,Date/Time,Profit\n" +
    "Exit long,1/5/2025 10:00,100\n" +
    "Exit short,1/5/2025 14:00,-40\n" +
    "Exit long,1/6/2025 10:00,80\n"
  )));
  assert.deepEqual(daily, [
    { date: "2025-01-05", pnl: 60 },
    { date: "2025-01-06", pnl: 80 },
  ]);
});

test("unparseable rows return no trades", () => {
  assert.equal(parseTV(rows("foo,bar\n1,2\n")).length, 0);
});
