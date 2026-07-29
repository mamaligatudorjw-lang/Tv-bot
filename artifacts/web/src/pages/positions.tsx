import { useState } from "react";
import { Link } from "wouter";
import { usePositions } from "@/hooks/use-api";
import type { Position } from "@/types/api";
import { formatPrice, formatTimeAgo } from "@/lib/format";

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
  entry,
  current,
  sl,
  tp,
  direction,
}: {
  entry: number;
  current: number | null;
  sl: number;
  tp: number;
  direction: "LONG" | "SHORT";
}) {
  if (current === null) return null;

  // For visual bar: map range [sl .. tp] to 0..100%
  const rangeTotal = Math.abs(tp - sl);
  if (rangeTotal === 0) return null;

  const pos = ((current - sl) / rangeTotal) * 100;
  const entryPos = ((entry - sl) / rangeTotal) * 100;
  const clampedPos = Math.max(0, Math.min(100, pos));

  const isPositive =
    direction === "LONG" ? current > entry : current < entry;

  return (
    <div className="mt-2 relative h-1.5 w-full rounded-full bg-white/10">
      {/* Entry marker */}
      <div
        className="absolute top-1/2 -translate-y-1/2 h-3 w-0.5 bg-slate-400 rounded-full"
        style={{ left: `${Math.max(0, Math.min(100, entryPos))}%` }}
        title="Вход"
      />
      {/* Current fill */}
      <div
        className={`absolute top-0 h-full rounded-full transition-all ${
          isPositive ? "bg-emerald-500" : "bg-rose-500"
        }`}
        style={
          direction === "LONG"
            ? { left: `${Math.max(0, Math.min(100, entryPos))}%`, width: `${Math.max(0, clampedPos - entryPos)}%` }
            : { left: `${Math.max(0, clampedPos)}%`, width: `${Math.max(0, entryPos - clampedPos)}%` }
        }
      />
      {/* Current price dot */}
      <div
        className={`absolute top-1/2 -translate-y-1/2 h-3 w-3 -translate-x-1/2 rounded-full border-2 border-background ${
          isPositive ? "bg-emerald-400" : "bg-rose-400"
        } transition-all`}
        style={{ left: `${clampedPos}%` }}
      />
      {/* SL label */}
      <span className="absolute -bottom-4 left-0 text-[10px] text-rose-400 font-mono">
        SL {formatPrice(sl)}
      </span>
      {/* TP label */}
      <span className="absolute -bottom-4 right-0 text-[10px] text-emerald-400 font-mono">
        TP {formatPrice(tp)}
      </span>
    </div>
  );
}

function PositionCard({ pos }: { pos: Position }) {
  const pnl = pos.pnlPct;
  const isLong = pos.direction === "LONG";

  return (
    <div className="rounded-xl border border-white/8 bg-white/[0.03] p-4 hover:bg-white/[0.05] transition-colors">
      {/* Header row */}
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <span
            className={`inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-xs font-semibold uppercase tracking-wide ${
              isLong
                ? "border-emerald-400/40 bg-emerald-400/10 text-emerald-400"
                : "border-rose-400/40 bg-rose-400/10 text-rose-400"
            }`}
          >
            {isLong ? "↑" : "↓"} {pos.direction}
          </span>
          <span className="font-mono font-semibold text-slate-100">
            {pos.symbol.replace("USDT", "")}
            <span className="text-slate-500 font-normal text-xs">/USDT</span>
          </span>
        </div>
        <div className="text-right">
          <div className={`text-lg leading-tight tabular-nums ${pnlClass(pnl)}`}>
            {pnl !== null ? `${pnl >= 0 ? "+" : ""}${pnl.toFixed(2)}%` : "—"}
          </div>
          <div className="text-xs text-slate-500">{formatElapsed(pos.elapsedSeconds)} назад</div>
        </div>
      </div>

      {/* Price row */}
      <div className="mt-3 grid grid-cols-2 gap-3 text-sm">
        <div>
          <div className="text-xs text-slate-500 mb-0.5">Вход</div>
          <div className="font-mono tabular-nums text-slate-200">{formatPrice(pos.entry)}</div>
        </div>
        <div>
          <div className="text-xs text-slate-500 mb-0.5">Сейчас</div>
          <div className={`font-mono tabular-nums ${pnlClass(pnl)}`}>
            {pos.currentPrice !== null ? formatPrice(pos.currentPrice) : "—"}
          </div>
        </div>
      </div>

      {/* Progress bar */}
      <div className="mt-3 mb-6">
        <ProgressBar
          entry={pos.entry}
          current={pos.currentPrice}
          sl={pos.slPrice}
          tp={pos.tpPrice}
          direction={pos.direction}
        />
      </div>
    </div>
  );
}

export default function Positions() {
  const { data, isLoading, error, isFetching } = usePositions();

  return (
    <div className="min-h-screen bg-background text-foreground">
      {/* Header */}
      <header className="sticky top-0 z-10 border-b border-white/5 bg-background/80 backdrop-blur">
        <div className="mx-auto flex max-w-7xl items-center justify-between gap-4 px-4 py-3">
          <div className="flex items-center gap-3">
            <h1 className="text-lg font-semibold tracking-tight">Crypto Signals</h1>
            <nav className="flex items-center gap-1 text-sm">
              <Link
                href="/"
                className="rounded-md px-3 py-1.5 text-slate-400 hover:text-slate-200 transition-colors"
              >
                Лента
              </Link>
              <Link
                href="/performance"
                className="rounded-md px-3 py-1.5 text-slate-400 hover:text-slate-200 transition-colors"
              >
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
            <p className="text-xs text-slate-600">
              Позиции появятся автоматически при срабатывании сигнала
            </p>
          </div>
        ) : (
          <>
            <div className="mb-4 flex items-center justify-between">
              <h2 className="text-sm font-medium text-slate-400">
                Открытых позиций: <span className="text-slate-200 font-semibold">{data.count}</span>
              </h2>
              <span className="text-xs text-slate-600">Обновляется каждые 15с</span>
            </div>
            <div className="space-y-3">
              {data.positions.map((pos) => (
                <PositionCard key={pos.id} pos={pos} />
              ))}
            </div>
          </>
        )}
      </main>
    </div>
  );
}
