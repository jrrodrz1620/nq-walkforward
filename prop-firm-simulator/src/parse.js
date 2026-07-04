// TradingView "List of Trades" parsing + daily stats. Kept free of JSX so it
// can be unit-tested in Node (see test/parse.test.mjs). Mirrors the format
// handling in ../dataio.py: old single-row exports, old two-row Entry/Exit
// exports (profit on the exit row only), and the new 2024+ format
// ("Trade number" / "Net PnL USD", with net PnL repeated on both rows).

const norm = k => k?.trim().toLowerCase().replace(/[^a-z0-9]/g, "");

function makeFind(row) {
  const keys = Object.keys(row);
  return (...pats) => {
    for (const p of pats) {
      const k = keys.find(k => norm(k).includes(p));
      if (k && row[k] !== undefined && row[k] !== "") return row[k].toString().trim();
    }
    return undefined;
  };
}

function parseDate(raw) {
  const dp = raw.trim().split(/[\s,T]/)[0];
  if (/^\d{4}-\d{2}-\d{2}$/.test(dp)) return dp;
  if (/^\d{1,2}\/\d{1,2}\/\d{4}$/.test(dp)) {
    const [m, d, y] = dp.split("/");
    return `${y}-${m.padStart(2, "0")}-${d.padStart(2, "0")}`;
  }
  const d = new Date(raw);
  return isNaN(d) ? null : d.toISOString().split("T")[0];
}

export function parseTV(rows) {
  const parsed = [];
  for (const r of rows) {
    const find = makeFind(r);
    const rP = find("netplusd", "netpnl", "profit", "pnl", "pl", "net", "gain", "return");
    const rD = find("dateandtime", "datetime", "date", "time", "timestamp");
    const rT = (find("type", "signal", "direction", "side") ?? "").toLowerCase();
    const rN = find("tradenumber", "tradenum", "trade");
    if (!rD) continue;
    const date = parseDate(rD);
    if (!date) continue;
    const profit = rP === undefined ? NaN : parseFloat(rP.replace(/[^0-9.\-]/g, ""));
    parsed.push({ num: rN, type: rT, date, profit });
  }

  // Two-row-per-trade exports repeat the trade number on the Entry and Exit
  // rows. Collapse each pair to one trade: net PnL from whichever row carries
  // it (exit row in the old format, both rows in the new one), dated by the
  // exit row since that's when the P&L hits the account balance.
  const nums = parsed.filter(p => p.num !== undefined).map(p => p.num);
  if (new Set(nums).size < nums.length) {
    const byNum = new Map();
    let orphan = 0;
    for (const p of parsed) {
      const k = p.num ?? `__orphan_${orphan++}`;
      if (!byNum.has(k)) byNum.set(k, []);
      byNum.get(k).push(p);
    }
    const trades = [];
    for (const grp of byNum.values()) {
      const profit = grp.map(p => p.profit).find(v => !isNaN(v));
      if (profit === undefined) continue;
      const exit = grp.find(p => p.type.includes("exit") || p.type.includes("close"));
      trades.push({ date: (exit ?? grp[0]).date, profit });
    }
    return trades.sort((a, b) => a.date.localeCompare(b.date));
  }

  // Single-row-per-trade exports; drop entry-marker rows with no realized P&L.
  return parsed
    .filter(p => !isNaN(p.profit) && !((p.type.includes("entry") || p.type.includes("open")) && p.profit === 0))
    .map(({ date, profit }) => ({ date, profit }));
}

export function groupByDay(trades) {
  const map = {};
  for (const t of trades) map[t.date] = (map[t.date] || 0) + t.profit;
  return Object.entries(map).sort((a, b) => a[0].localeCompare(b[0])).map(([date, pnl]) => ({ date, pnl }));
}

export function calcStats(daily) {
  if (!daily.length) return null;
  const wins = daily.filter(d => d.pnl > 0), losses = daily.filter(d => d.pnl < 0);
  const gW = wins.reduce((s, d) => s + d.pnl, 0), gL = Math.abs(losses.reduce((s, d) => s + d.pnl, 0));
  const total = daily.reduce((s, d) => s + d.pnl, 0);
  let peak = 0, bal = 0, maxDD = 0;
  for (const d of daily) { bal += d.pnl; if (bal > peak) peak = bal; if (peak - bal > maxDD) maxDD = peak - bal; }
  return { winRate: wins.length / daily.length, profitFactor: gL > 0 ? gW / gL : Infinity, totalPnL: total, maxDD, avgDailyPnL: total / daily.length, bestDay: Math.max(...daily.map(d => d.pnl)), worstDay: Math.min(...daily.map(d => d.pnl)) };
}
