import { Link } from "wouter";
import { usePositions } from "@/hooks/use-api";
import type { Position } from "@/types/api";
import { formatPrice } from "@/lib/format";

function formatElapsed(seconds: number): string {
  if (seconds < 3600) return `${Math.floor(seconds / 60)}м`;
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return m > 0 ? `${h}ч ${m}м` : `${h}ч`;
}

function pnlClass(v: number | null): string {
  if (v === null) return "text-slate-500";
  if (v > 0) return "text-emerald-400 font-semibold";
  if (v < 0) return "text-rose-400 font-semibold";
  return "text-slate-400";
}

function ProgressBar({
  entry, current, sl, tp, direction,
}: {
  entry: number; current: number | null; sl: number; tp: number; direction: "LONG" | "SHORT";
}) {
  if (current === null) return null;
  const rangeTotal = Math.abs(tp - sl);
  if (rangeTotal === 0) return null;
  const pos = ((current - sl) / rangeTotal) * 100;
  const entryPos = ((entry - sl) / rangeTotal) * 100;
  const clampedPos = Math.max(0, Math.min(100, pos));
  const isPositive = direction === "LONG" ? current > entry : current < entry;
  return (
    <div className="mt-2 relative h-1.5 w-full rounded-full bg-white/10">
      <div
        className="absolute top-1/2 -translate-y-1/2 h-3 w-0.5 bg-slate-400 rounded-full"
        style={{ left: `${Math.max(0, Math.min(100, entryPos))}%` }}
      />
      <div
        className={`absolute top-0 h-full rounded-full transition-all ${isPositive ? "bg-emerald-500" : "bg-rose-500"}`}
        style={
          direction === "LONG"
            ? { left: `${Math.max(0, Math.min(100, entryPos))}%`, width: `${Math.max(0, clampedPos - entryPos)}%` }
            : { left: `${Math.max(0, clampedPos)}%`, width: `${Math.max(0, entryPos - clampedPos)}%` }
        }
      />
      <div
        className={`absolute top-1/2 -translate-y-1/2 h-3 w-3 -translate-x-1/2 rounded-full border-2 border-background ${isPositive ? "bg-emerald-400" : "bg-rose-400"} transition-all`}
        style={{ left: `${clampedPos}%` }}
      />
      <span className="absolute -bottom-4 left-0 text-[10px] text-rose-400 font-mono">SL {formatPrice(sl)}</span>
      <span className="absolute -bottom-4 right-0 text-[10px] text-emerald-400 font-mono">TP {formatPrice(tp)}</span>
    </div>
  );
}

function PositionCard({ pos }: { pos: Position }) {
  const pnl = pos.pnlPct;
  const isLong = pos.direction === "LONG";
  return (
    <div className="rounded-xl border border-white/8 bg-white/[0.03] p-4 hover:bg-white/[0.05] transition-colors">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <span className="font-mono font-semibold text-slate-100 text-sm">
            {pos.symbol.replace("USDT", "")}
            <span className="text-slate-500 font-normal text-xs">/USDT</span>
          </span>
        </div>
        <div className="flex items-center gap-4">
          <div className="text-right text-xs text-slate-500">{formatElapsed(pos.elapsedSeconds)}</div>
          <div className={`text-base leading-tight tabular-nums text-right ${pnlClass(pnl)}`}>
            {pnl !== null ? `${pnl >= 0 ? "+" : ""}${pnl.toFixed(2)}%` : "—"}
          </div>
        </div>
      </div>
      <div className="mt-2 grid grid-cols-2 gap-3 text-sm">
        <div>
          <div className="text-xs text-slate-500 mb-0.5">Вход</div>
          <div className="font-mono tabular-nums text-slate-300 text-xs">{formatPrice(pos.entry)}</div>
        </div>
        <div>
          <div className="text-xs text-slate-500 mb-0.5">Сейчас</div>
          <div className={`font-mono tabular-nums text-xs ${pnlClass(pnl)}`}>
            {pos.currentPrice !== null ? formatPrice(pos.currentPrice) : "—"}
          </div>
        </div>
      </div>
      <div className="mt-3 mb-6">
        <ProgressBar entry={pos.entry} current={pos.currentPrice} sl={pos.slPrice} tp={pos.tpPrice} direction={pos.direction} />
      </div>
    </div>
  );
}

interface GroupSummary {
  count: number;
  inProfit: number;
  inLoss: number;
  sumProfit: number;  // sum of positive pnlPct
  sumLoss: number;    // sum of negative pnlPct (negative value)
  netPnl: number;     // sumProfit + sumLoss
  withPrice: number;  // positions with a known current price
}

function calcSummary(positions: Position[]): GroupSummary {
  let inProfit = 0, inLoss = 0, sumProfit = 0, sumLoss = 0, withPrice = 0;
  for (const p of positions) {
    if (p.pnlPct === null) continue;
    withPrice++;
    if (p.pnlPct > 0) { inProfit++; sumProfit += p.pnlPct; }
    else if (p.pnlPct < 0) { inLoss++; sumLoss += p.pnlPct; }
  }
  return {
    count: positions.length,
    inProfit,
    inLoss,
    sumProfit,
    sumLoss,
    netPnl: sumProfit + sumLoss,
    withPrice,
  };
}

function SummaryBanner({
  direction,
  summary,
}: {
  direction: "LONG" | "SHORT";
  summary: GroupSummary;
}) {
  const isLong = direction === "LONG";
  const accentPos = isLong ? "text-emerald-400" : "text-emerald-400";
  const accentNeg = "text-rose-400";
  const netPositive = summary.netPnl > 0;
  const netClass = netPositive ? "text-emerald-400" : summary.netPnl < 0 ? "text-rose-400" : "text-slate-400";
  const netLabel = netPositive ? "В прибыли" : summary.netPnl < 0 ? "В убытке" : "Безубыток";
  const borderColor = isLong ? "border-emerald-400/20" : "border-rose-400/20";
  const bgColor = isLong ? "bg-emerald-400/[0.04]" : "bg-rose-400/[0.04]";

  return (
    <div className={`rounded-2xl border ${borderColor} ${bgColor} px-5 py-4 mb-3`}>
      {/* Title row */}
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <span
            className={`inline-flex items-center gap-1 rounded-md border px-2.5 py-1 text-xs font-bold uppercase tracking-widest ${
              isLong
                ? "border-emerald-400/40 bg-emerald-400/10 text-emerald-400"
                : "border-rose-400/40 bg-rose-400/10 text-rose-400"
            }`}
          >
            {isLong ? "↑" : "↓"} {direction}
          </span>
          <span className="text-sm text-slate-400 font-medium">
            {summary.count} поз.
            {summary.withPrice < summary.count && (
              <span className="text-xs text-slate-600 ml-1">({summary.withPrice} с ценой)</span>
            )}
          </span>
        </div>
        <div className={`text-right`}>
          <div className={`font-mono font-bold text-lg tabular-nums ${netClass}`}>
            {summary.netPnl >= 0 ? "+" : ""}{summary.netPnl.toFixed(2)}%
          </div>
          <div className={`text-xs font-medium ${netClass}`}>{netLabel}</div>
        </div>
      </div>

      {/* Stat row */}
      <div className="grid grid-cols-3 gap-3 border-t border-white/5 pt-3">
        <div>
          <div className="text-[10px] uppercase tracking-wider text-slate-500 mb-0.5">
            Прибыльных
          </div>
          <div className={`font-mono text-sm font-semibold tabular-nums ${accentPos}`}>
            {summary.inProfit}
          </div>
          <div className={`font-mono text-xs tabular-nums ${accentPos}`}>
            {summary.sumProfit > 0 ? `+${summary.sumProfit.toFixed(2)}%` : "—"}
          </div>
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-wider text-slate-500 mb-0.5">
            Убыточных
          </div>
          <div className={`font-mono text-sm font-semibold tabular-nums ${accentNeg}`}>
            {summary.inLoss}
          </div>
          <div className={`font-mono text-xs tabular-nums ${accentNeg}`}>
            {summary.sumLoss < 0 ? `${summary.sumLoss.toFixed(2)}%` : "—"}
          </div>
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-wider text-slate-500 mb-0.5">
            Итог
          </div>
          <div className={`font-mono text-sm font-bold tabular-nums ${netClass}`}>
            {summary.netPnl >= 0 ? "+" : ""}{summary.netPnl.toFixed(2)}%
          </div>
          <div className={`text-xs font-semibold ${netClass}`}>{netLabel}</div>
        </div>
      </div>
    </div>
  );
}

function DirectionGroup({
  direction,
  positions,
}: {
  direction: "LONG" | "SHORT";
  positions: Position[];
}) {
  if (positions.length === 0) return null;
  const summary = calcSummary(positions);
  return (
    <section className="mb-8">
      <SummaryBanner direction={direction} summary={summary} />
      <div className="space-y-2 mt-3">
        {positions.map((pos) => (
          <PositionCard key={pos.id} pos={pos} />
        ))}
      </div>
    </section>
  );
}

export default function Positions() {
  const { data, isLoading, error, isFetching } = usePositions();

  const longs  = data?.positions.filter((p) => p.direction === "LONG")  ?? [];
  const shorts = data?.positions.filter((p) => p.direction === "SHORT") ?? [];

  return (
    <div className="min-h-screen bg-background text-foreground">
      <header className="sticky top-0 z-10 border-b border-white/5 bg-background/80 backdrop-blur">
        <div className="mx-auto flex max-w-7xl items-center justify-between gap-4 px-4 py-3">
          <div className="flex items-center gap-3">
            <h1 className="text-lg font-semibold tracking-tight">Crypto Signals</h1>
            <nav className="flex items-center gap-1 text-sm">
              <Link href="/" className="rounded-md px-3 py-1.5 text-slate-400 hover:text-slate-200 transition-colors">
                Лента
              </Link>
              <Link href="/performance" className="rounded-md px-3 py-1.5 text-slate-400 hover:text-slate-200 transition-colors">
                Производительность
              </Link>
              <span className="rounded-md bg-primary/10 px-3 py-1.5 font-medium text-primary">
                Позиции
              </span>
            </nav>
          </div>
          {isFetching && !isLoading && (
            <div className="h-1 w-16 overflow-hidden rounded-full bg-white/10">
              <div className="h-full w-1/2 animate-[slide_1s_ease-in-out_infinite] rounded-full bg-primary" />
            </div>
          )}
        </div>
      </header>

      <main className="mx-auto max-w-3xl px-4 py-6">
        {isLoading ? (
          <div className="space-y-3">
            {[1, 2, 3].map((i) => (
              <div key={i} className="h-32 rounded-xl bg-white/[0.03] animate-pulse" />
            ))}
          </div>
        ) : error ? (
          <div className="rounded-xl border border-destructive/30 bg-destructive/10 p-6 text-center text-sm text-destructive">
            Не удалось загрузить позиции
          </div>
        ) : !data?.positions.length ? (
          <div className="flex flex-col items-center justify-center gap-3 py-20 text-center">
            <div className="text-4xl opacity-40">📭</div>
            <p className="text-slate-400 text-sm">Нет открытых позиций</p>
            <p className="text-xs text-slate-600">Позиции появятся автоматически при срабатывании сигнала</p>
          </div>
        ) : (
          <>
            {/* Overall summary */}
            <div className="mb-6 flex items-center justify-between">
              <h2 className="text-sm font-medium text-slate-400">
                Всего позиций:{" "}
                <span className="text-slate-200 font-semibold">{data.count}</span>
                {longs.length > 0 && (
                  <span className="ml-2 text-emerald-400/80">↑{longs.length} LONG</span>
                )}
                {shorts.length > 0 && (
                  <span className="ml-2 text-rose-400/80">↓{shorts.length} SHORT</span>
                )}
              </h2>
              <span className="text-xs text-slate-600">Обновляется каждые 15с</span>
            </div>

            <DirectionGroup direction="LONG"  positions={longs}  />
            <DirectionGroup direction="SHORT" positions={shorts} />
          </>
        )}
      </main>
    </div>
  );
}
