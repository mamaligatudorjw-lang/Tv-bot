export function formatPrice(price: number): string {
  return new Intl.NumberFormat("ru-RU", {
    minimumFractionDigits: price < 1 ? 4 : 2,
    maximumFractionDigits: price < 1 ? 6 : 2,
  }).format(price);
}

export function formatTimeAgo(ts: number | null | undefined): string {
  if (ts === null || ts === undefined || !Number.isFinite(ts)) return "—";
  const normalized = ts > 1e12 ? ts / 1000 : ts;
  const diffInSeconds = Math.floor(Date.now() / 1000 - normalized);
  if (!Number.isFinite(diffInSeconds)) return "—";
  const rtf = new Intl.RelativeTimeFormat("ru-RU", { numeric: "auto" });
  if (diffInSeconds < 60) return "только что";
  if (diffInSeconds < 3600) return rtf.format(-Math.floor(diffInSeconds / 60), "minute");
  if (diffInSeconds < 86400) return rtf.format(-Math.floor(diffInSeconds / 3600), "hour");
  return rtf.format(-Math.floor(diffInSeconds / 86400), "day");
}

export function formatSymbol(symbol: string): { base: string; quote: string } {
  if (symbol.endsWith("USDT")) {
    return { base: symbol.slice(0, -4), quote: "USDT" };
  }
  return { base: symbol, quote: "" };
}

export function calcDeltaPercent(current: number | null, base: number): number | null {
  if (current === null || base === 0) return null;
  return ((current - base) / base) * 100;
}

export function formatDelta(delta: number | null): string {
  if (delta === null) return "—";
  const sign = delta > 0 ? "+" : "";
  return `${sign}${delta.toFixed(2)}%`;
}
