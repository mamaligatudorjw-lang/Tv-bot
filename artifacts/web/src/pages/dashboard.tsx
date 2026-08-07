import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "wouter";
import { useBotStatus, useSignals, useStats, useAnalytics, usePositions, usePerformance } from "@/hooks/use-api";
import type { Position, Signal } from "@/types/api";
import {
  calcDeltaPercent,
  formatDelta,
  formatPrice,
  formatSymbol,
  formatTimeAgo,
} from "@/lib/format";

// ── AI Analyze widget ────────────────────────────────────────────────────────

function AiAnalyzeWidget() {
  const [symbol, setSymbol] = useState("");
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<{
    symbol: string; price: number; pct24: number;
    rsi: number | null; trend: string; analysis: string;
  } | null>(null);
  const [error, setError] = useState<string | null>(null);

  const analyze = async () => {
    const sym = symbol.trim().toUpperCase();
    if (!sym) return;
    setLoading(true); setResult(null); setError(null);
    try {
      const res = await fetch("/bot-api/ai-analyze", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbol: sym }),
      });
      const data = await res.json();
      if (!res.ok) { setError(data.error || "Ошибка"); return; }
      setResult(data);
    } catch {
      setError("Не удалось подключиться к серверу");
    } finally {
      setLoading(false);
    }
  };

  return (
    <section className="mt-8">
      <div className="mb-3 flex items-center gap-2">
        <span className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">
          🤖 AI-анализ монеты
        </span>
      </div>

      <div className="rounded-2xl border border-card-border bg-card/60 p-4">
        <div className="flex gap-2">
          <input
            type="text"
            placeholder="BTCUSDT, ETHUSDT…"
            value={symbol}
            onChange={(e) => setSymbol(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && analyze()}
            className="flex-1 rounded-xl border border-card-border bg-background px-3 py-2 text-sm outline-none focus:border-primary/50"
          />
          <button
            onClick={analyze}
            disabled={loading || !symbol.trim()}
            className="rounded-xl bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-40 transition-opacity"
          >
            {loading ? "…" : "Анализировать"}
          </button>
        </div>

        {error && (
          <div className="mt-3 rounded-xl border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive">
            {error}
          </div>
        )}

        {loading && (
          <div className="mt-4 space-y-2">
            {[1,2,3].map(i => (
              <div key={i} className="h-4 w-full animate-pulse rounded bg-muted/40" />
            ))}
          </div>
        )}

        {result && (
          <div className="mt-4">
            <div className="mb-2 flex flex-wrap gap-3 text-xs text-muted-foreground">
              <span className="font-mono font-semibold text-foreground">{result.symbol}</span>
              <span>${result.price.toPrecision(5)}</span>
              <span className={result.pct24 >= 0 ? "text-emerald-400" : "text-red-400"}>
                {result.pct24 >= 0 ? "+" : ""}{result.pct24.toFixed(2)}%
              </span>
              {result.rsi != null && <span>RSI {result.rsi.toFixed(1)}</span>}
              <span className="text-muted-foreground/60">{result.trend}</span>
            </div>
            <div className="whitespace-pre-wrap rounded-xl border border-card-border bg-background/50 px-4 py-3 text-sm leading-relaxed">
              {result.analysis}
            </div>
          </div>
        )}
      </div>
    </section>
  );
}

// ── Analytics widget ────────────────────────────────────────────────────────

const PAGE_LABELS: Record<string, string> = {
  "/":            "Лента",
  "/performance": "Производительность",
  "/positions":   "Позиции",
};

function AnalyticsWidget() {
  const { data, isLoading } = useAnalytics();

  const bar = (v: number, max: number) => {
    const pct = max > 0 ? Math.round((v / max) * 100) : 0;
    return (
      <div className="mt-1 h-1 w-full rounded-full bg-muted/40">
        <div className="h-1 rounded-full bg-primary/60" style={{ width: `${pct}%` }} />
      </div>
    );
  };

  const maxViews = Math.max(...(data?.daily ?? []).map((d) => d.views), 1);

  return (
    <section className="mt-8">
      <div className="mb-3 flex items-center gap-2">
        <span className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">
          Посетители сайта
        </span>
        <span className="rounded-full border border-card-border px-2 py-0.5 text-[10px] text-muted-foreground/60">
          данные с этой страницы
        </span>
      </div>

      {/* KPI row */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {(
          [
            { label: "Сегодня",           period: "1d",  key: "unique"    as const },
            { label: "Повторные / 7д",    period: "7d",  key: "returning" as const },
            { label: "Уникальных / 7д",   period: "7d",  key: "unique"    as const },
            { label: "Просмотров / 30д",  period: "30d", key: "views"     as const },
          ] as const
        ).map(({ label, period, key }) => {
          const val = isLoading ? null : (data as any)?.[period]?.[key] ?? 0;
          const sub =
            key === "returning" && data
              ? `${(data as any)[period]?.returnRate ?? 0}% от уникальных`
              : key === "unique" && period === "7d" && data
              ? `из ${data["7d"].views} просмотров`
              : undefined;
          return (
            <div
              key={`${period}-${key}`}
              className="rounded-2xl border border-card-border bg-card/60 p-4 backdrop-blur-sm"
            >
              <div className="text-[11px] uppercase tracking-[0.15em] text-muted-foreground">
                {label}
              </div>
              <div className="mt-2 font-mono text-2xl tabular-nums leading-none">
                {val === null ? "—" : val.toLocaleString("ru-RU")}
              </div>
              {sub && (
                <div className="mt-1.5 text-[11px] text-muted-foreground">{sub}</div>
              )}
            </div>
          );
        })}
      </div>

      {/* Daily sparkline + page breakdown */}
      <div className="mt-3 grid gap-3 sm:grid-cols-2">
        {/* Sparkline — 14 days */}
        <div className="rounded-2xl border border-card-border bg-card/40 p-4 backdrop-blur-sm">
          <div className="mb-3 text-[11px] uppercase tracking-[0.15em] text-muted-foreground">
            Активность (14 дней)
          </div>
          {isLoading ? (
            <div className="h-20 animate-pulse rounded-lg bg-muted/30" />
          ) : (
            <div className="space-y-1">
              {(data?.daily ?? []).slice(-14).map((d) => (
                <div key={d.date} className="flex items-center gap-2">
                  <span className="w-16 text-right font-mono text-[10px] text-muted-foreground/70">
                    {d.date.slice(5)}
                  </span>
                  <div className="flex-1">
                    {bar(d.views, maxViews)}
                  </div>
                  <span className="w-6 text-right font-mono text-[10px] tabular-nums text-muted-foreground">
                    {d.views}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Page breakdown */}
        <div className="rounded-2xl border border-card-border bg-card/40 p-4 backdrop-blur-sm">
          <div className="mb-3 text-[11px] uppercase tracking-[0.15em] text-muted-foreground">
            Страницы (30 дней)
          </div>
          {isLoading ? (
            <div className="space-y-2">
              {[1,2,3].map(i => (
                <div key={i} className="h-6 animate-pulse rounded bg-muted/30" />
              ))}
            </div>
          ) : (data?.pages ?? []).length === 0 ? (
            <p className="text-sm text-muted-foreground">Нет данных пока</p>
          ) : (
            <div className="space-y-2">
              {(data?.pages ?? []).map((p) => {
                const maxP = Math.max(...(data?.pages ?? []).map(x => x.views), 1);
                return (
                  <div key={p.page}>
                    <div className="flex items-center justify-between">
                      <span className="font-mono text-xs text-foreground/80">
                        {PAGE_LABELS[p.page] ?? p.page}
                      </span>
                      <span className="font-mono text-xs tabular-nums text-muted-foreground">
                        {p.views}
                      </span>
                    </div>
                    {bar(p.views, maxP)}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </section>
  );
}

// ── Main page ─────────────────────────────────────────────────────────────────

type SideFilter = "all" | "buy" | "sell";

const FILTERS: { id: SideFilter; label: string }[] = [
  { id: "all", label: "Все" },
  { id: "buy", label: "Покупка" },
  { id: "sell", label: "Продажа" },
];

function StatusPill() {
  const { data, isLoading, isError } = useBotStatus();
  const tone = isError
    ? "bg-destructive/10 text-destructive border-destructive/30"
    : isLoading
    ? "bg-muted text-muted-foreground border-border"
    : data?.silenced
    ? "bg-yellow-400/10 text-yellow-300 border-yellow-400/30"
    : "bg-success/10 text-success border-success/30";
  const dot = isError
    ? "bg-destructive"
    : isLoading
    ? "bg-muted-foreground"
    : data?.silenced
    ? "bg-yellow-400"
    : "bg-success";
  const text = isError
    ? "Бот недоступен"
    : isLoading
    ? "Подключение…"
    : data?.silenced
    ? "Алерты приостановлены"
    : data?.initialized
    ? "В эфире"
    : "Инициализация";
  return (
    <div
      className={`inline-flex items-center gap-2 rounded-full border px-3 py-1 text-xs font-medium ${tone}`}
      data-testid="status-pill"
    >
      <span className={`relative inline-flex h-2 w-2`}>
        <span
          className={`absolute inline-flex h-full w-full rounded-full opacity-60 ${dot} animate-ping`}
        />
        <span className={`relative inline-flex h-2 w-2 rounded-full ${dot}`} />
      </span>
      <span>{text}</span>
      {data?.trackedPairs ? (
        <>
          <span className="opacity-40">·</span>
          <span className="font-mono tabular-nums">
            {data.trackedPairs} пар
          </span>
        </>
      ) : null}
      {data?.lastRun ? (
        <>
          <span className="opacity-40">·</span>
          <span className="opacity-80">{formatTimeAgo(data.lastRun)}</span>
        </>
      ) : null}
    </div>
  );
}

function KpiCard({
  label,
  value,
  hint,
  tone = "default",
}: {
  label: string;
  value: string;
  hint?: string;
  tone?: "default" | "buy" | "sell";
}) {
  const toneClass =
    tone === "buy"
      ? "text-success"
      : tone === "sell"
      ? "text-destructive"
      : "text-foreground";
  return (
    <div className="rounded-2xl border border-card-border bg-card/60 p-5 backdrop-blur-sm">
      <div className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">
        {label}
      </div>
      <div
        className={`mt-2 font-mono text-3xl tabular-nums leading-none ${toneClass}`}
      >
        {value}
      </div>
      {hint ? (
        <div className="mt-2 text-xs text-muted-foreground">{hint}</div>
      ) : null}
    </div>
  );
}

function DeltaCell({ pct }: { pct: number | null }) {
  if (pct === null) {
    return <span className="text-muted-foreground/50">—</span>;
  }
  const tone =
    pct > 0.05
      ? "text-success"
      : pct < -0.05
      ? "text-destructive"
      : "text-muted-foreground";
  return (
    <span className={`font-mono tabular-nums text-xs ${tone}`}>
      {formatDelta(pct)}
    </span>
  );
}

function SideBadge({ side }: { side: Signal["side"] }) {
  const map = {
    buy: {
      label: "Покупка",
      cls: "bg-success/15 text-success ring-1 ring-success/30",
    },
    sell: {
      label: "Продажа",
      cls: "bg-destructive/15 text-destructive ring-1 ring-destructive/30",
    },
    neutral: {
      label: "Нейтрально",
      cls: "bg-muted text-muted-foreground ring-1 ring-border",
    },
  } as const;
  const m = map[side];
  return (
    <span
      className={`inline-flex items-center rounded-md px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wider ${m.cls}`}
    >
      {m.label}
    </span>
  );
}

function ScoreBadge({ score }: { score: number | null }) {
  if (score == null || !Number.isFinite(score)) return null;
  const cls =
    score >= 75
      ? "bg-success/15 text-success border-success/30"
      : score >= 60
      ? "bg-primary/15 text-primary border-primary/30"
      : score >= 45
      ? "bg-muted text-foreground border-card-border"
      : "bg-destructive/10 text-destructive border-destructive/30";
  return (
    <span
      title="Сила сигнала (0–100): историческая точность типа, RSI, тренд, объём, согласованность с BTC"
      className={`inline-flex items-center rounded-md border px-1.5 py-0.5 text-[10px] font-semibold tabular-nums ${cls}`}
    >
      {score}/100
    </span>
  );
}

function SignalRow({ s, isNew }: { s: Signal; isNew: boolean }) {
  const sym = formatSymbol(s.symbol);
  const d15 = calcDeltaPercent(s.price15m, s.priceAtAlert);
  const d1h = calcDeltaPercent(s.price1h, s.priceAtAlert);
  const d4h = calcDeltaPercent(s.price4h, s.priceAtAlert);
  const accent =
    s.side === "buy"
      ? "before:bg-success"
      : s.side === "sell"
      ? "before:bg-destructive"
      : "before:bg-muted-foreground/40";
  return (
    <div
      className={`group relative overflow-hidden rounded-xl border border-card-border bg-card/40 px-4 py-3 transition-colors hover:bg-card/70 before:absolute before:left-0 before:top-0 before:h-full before:w-[3px] ${accent} ${
        isNew ? "ring-1 ring-primary/40 animate-in fade-in slide-in-from-top-2 duration-500" : ""
      }`}
      data-testid={`signal-row-${s.id}`}
    >
      <div className="flex items-center gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-baseline gap-2">
            <span className="font-mono text-base font-semibold tracking-tight">
              {sym.base}
            </span>
            <span className="font-mono text-xs text-muted-foreground">
              / {sym.quote}
            </span>
            <SideBadge side={s.side} />
            <ScoreBadge score={s.score} />
          </div>
          <div className="mt-1 truncate text-xs text-muted-foreground">
            {s.alertTypeLabel}
            {s.recommendation && s.recommendation !== "NEUTRAL" ? (
              <>
                <span className="mx-1.5 opacity-40">·</span>
                <span className="font-medium">{s.recommendation}</span>
              </>
            ) : null}
          </div>
        </div>
        <div className="hidden shrink-0 text-right sm:block">
          <div className="font-mono text-sm tabular-nums">
            {formatPrice(s.priceAtAlert)}
          </div>
          <div className="text-[11px] text-muted-foreground">
            {formatTimeAgo(s.ts)}
          </div>
        </div>
      </div>
      <div className="mt-3 grid grid-cols-3 gap-2 border-t border-card-border/60 pt-2 sm:hidden">
        <div className="text-center">
          <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
            15м
          </div>
          <DeltaCell pct={d15} />
        </div>
        <div className="text-center">
          <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
            1ч
          </div>
          <DeltaCell pct={d1h} />
        </div>
        <div className="text-center">
          <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
            4ч
          </div>
          <DeltaCell pct={d4h} />
        </div>
      </div>
      <div className="ml-auto mt-2 hidden items-center justify-end gap-5 sm:flex">
        <div className="flex items-baseline gap-1.5">
          <span className="text-[10px] uppercase tracking-wider text-muted-foreground">
            15м
          </span>
          <DeltaCell pct={d15} />
        </div>
        <div className="flex items-baseline gap-1.5">
          <span className="text-[10px] uppercase tracking-wider text-muted-foreground">
            1ч
          </span>
          <DeltaCell pct={d1h} />
        </div>
        <div className="flex items-baseline gap-1.5">
          <span className="text-[10px] uppercase tracking-wider text-muted-foreground">
            4ч
          </span>
          <DeltaCell pct={d4h} />
        </div>
      </div>
      <div className="mt-2 text-right text-[10px] text-muted-foreground sm:hidden">
        {formatPrice(s.priceAtAlert)} · {formatTimeAgo(s.ts)}
      </div>
    </div>
  );
}

// ── Signal stats widget ──────────────────────────────────────────────────────

const STAT_WINDOWS = [
  { key: "1h",  label: "1ч"   },
  { key: "4h",  label: "4ч"   },
  { key: "8h",  label: "8ч"   },
  { key: "12h", label: "12ч"  },
  { key: "16h", label: "16ч"  },
  { key: "24h", label: "24ч"  },
  { key: "2d",  label: "2д"   },
  { key: "4d",  label: "4д"   },
  { key: "6d",  label: "6д"   },
  { key: "8d",  label: "8д"   },
  { key: "15d", label: "15д"  },
  { key: "30d", label: "30д"  },
];

function pct(v: number | null, d = 1) {
  if (v === null || !Number.isFinite(v)) return "—";
  return `${(v * 100).toFixed(d)}%`;
}

function SignalStatsWidget() {
  const [win, setWin] = useState("24h");
  const { data, isLoading } = usePerformance(win);

  // Aggregate buy / sell separately
  const agg = useMemo(() => {
    if (!data?.byType) return null;
    const sides = { buy: { count: 0, fu: 0, wins: 0, ret: 0 }, sell: { count: 0, fu: 0, wins: 0, ret: 0 } } as Record<string, { count: number; fu: number; wins: number; ret: number }>;
    for (const b of data.byType) {
      const s = sides[b.side];
      if (!s) continue;
      s.count += b.count;
      // prefer 4h horizon for quick feedback; fall back to 1h
      const h = b.horizons["4h"]?.followups ? b.horizons["4h"] : b.horizons["1h"];
      if (!h || !h.followups) continue;
      s.fu   += h.followups;
      s.wins += h.winRate  !== null ? h.winRate  * h.followups : 0;
      s.ret  += h.avgReturn !== null ? h.avgReturn * h.followups : 0;
    }
    return {
      total: data.totalSignals,
      buy: {
        count:   sides.buy.count,
        winRate: sides.buy.fu  ? sides.buy.wins / sides.buy.fu  : null,
        avgRet:  sides.buy.fu  ? sides.buy.ret  / sides.buy.fu  : null,
        fu:      sides.buy.fu,
      },
      sell: {
        count:   sides.sell.count,
        winRate: sides.sell.fu ? sides.sell.wins / sides.sell.fu : null,
        avgRet:  sides.sell.fu ? sides.sell.ret  / sides.sell.fu : null,
        fu:      sides.sell.fu,
      },
    };
  }, [data]);

  return (
    <section className="mt-4">
      <div className="rounded-2xl border border-card-border bg-card/40 p-4 backdrop-blur-sm">
        {/* header */}
        <div className="mb-3 flex items-center justify-between">
          <span className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">
            Статистика сигналов
          </span>
          <Link
            href="/performance"
            className="text-[11px] text-primary/70 hover:text-primary transition-colors"
          >
            Подробнее →
          </Link>
        </div>

        {/* period selector */}
        <div className="mb-4 flex flex-wrap gap-1">
          {STAT_WINDOWS.map((w) => (
            <button
              key={w.key}
              onClick={() => setWin(w.key)}
              className={`rounded-full px-3 py-1 text-xs font-medium transition-colors ${
                win === w.key
                  ? "bg-primary text-primary-foreground"
                  : "bg-white/5 text-slate-400 hover:bg-white/10 hover:text-slate-200"
              }`}
            >
              {w.label}
            </button>
          ))}
        </div>

        {/* stats */}
        {isLoading ? (
          <div className="grid grid-cols-3 gap-3">
            {[1,2,3].map(i => (
              <div key={i} className="h-16 animate-pulse rounded-xl bg-muted/30" />
            ))}
          </div>
        ) : !agg || agg.total === 0 ? (
          <div className="py-4 text-center text-sm text-muted-foreground">
            Нет данных за этот период
          </div>
        ) : (
          <div className="grid grid-cols-3 gap-3">
            {/* total */}
            <div className="rounded-xl border border-card-border bg-card/60 p-3 text-center">
              <div className="text-[10px] uppercase tracking-wider text-slate-500 mb-1">Всего</div>
              <div className="font-mono text-2xl font-semibold tabular-nums">{agg.total}</div>
              <div className="text-[10px] text-slate-500 mt-1">сигналов</div>
            </div>

            {/* buy */}
            <div className="rounded-xl border border-emerald-400/20 bg-emerald-400/[0.04] p-3">
              <div className="flex items-center gap-1.5 mb-2">
                <span className="text-[10px] uppercase tracking-wider font-bold text-emerald-400">↑ Покупка</span>
                <span className="text-[10px] text-slate-500">{agg.buy.count}</span>
              </div>
              <div className="grid grid-cols-2 gap-1 text-xs">
                <div>
                  <div className="text-[10px] text-slate-500">Win-rate</div>
                  <div className={`font-mono font-semibold tabular-nums ${agg.buy.winRate !== null && agg.buy.winRate >= 0.5 ? "text-emerald-400" : "text-rose-400"}`}>
                    {pct(agg.buy.winRate, 0)}
                  </div>
                </div>
                <div>
                  <div className="text-[10px] text-slate-500">Avg return</div>
                  <div className={`font-mono font-semibold tabular-nums ${agg.buy.avgRet !== null && agg.buy.avgRet >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
                    {agg.buy.avgRet !== null ? `${agg.buy.avgRet >= 0 ? "+" : ""}${pct(agg.buy.avgRet, 2)}` : "—"}
                  </div>
                </div>
              </div>
              {agg.buy.fu > 0 && (
                <div className="text-[10px] text-slate-600 mt-1">{agg.buy.fu} с данными 4ч</div>
              )}
            </div>

            {/* sell */}
            <div className="rounded-xl border border-rose-400/20 bg-rose-400/[0.04] p-3">
              <div className="flex items-center gap-1.5 mb-2">
                <span className="text-[10px] uppercase tracking-wider font-bold text-rose-400">↓ Продажа</span>
                <span className="text-[10px] text-slate-500">{agg.sell.count}</span>
              </div>
              <div className="grid grid-cols-2 gap-1 text-xs">
                <div>
                  <div className="text-[10px] text-slate-500">Win-rate</div>
                  <div className={`font-mono font-semibold tabular-nums ${agg.sell.winRate !== null && agg.sell.winRate >= 0.5 ? "text-emerald-400" : "text-rose-400"}`}>
                    {pct(agg.sell.winRate, 0)}
                  </div>
                </div>
                <div>
                  <div className="text-[10px] text-slate-500">Avg return</div>
                  <div className={`font-mono font-semibold tabular-nums ${agg.sell.avgRet !== null && agg.sell.avgRet >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
                    {agg.sell.avgRet !== null ? `${agg.sell.avgRet >= 0 ? "+" : ""}${pct(agg.sell.avgRet, 2)}` : "—"}
                  </div>
                </div>
              </div>
              {agg.sell.fu > 0 && (
                <div className="text-[10px] text-slate-600 mt-1">{agg.sell.fu} с данными 4ч</div>
              )}
            </div>
          </div>
        )}
      </div>
    </section>
  );
}

// ── Positions summary widget ─────────────────────────────────────────────────

function calcPosSummary(positions: Position[]) {
  let inProfit = 0, inLoss = 0, sumProfit = 0, sumLoss = 0;
  for (const p of positions) {
    if (p.pnlPct === null) continue;
    if (p.pnlPct > 0) { inProfit++; sumProfit += p.pnlPct; }
    else if (p.pnlPct < 0) { inLoss++; sumLoss += p.pnlPct; }
  }
  return { count: positions.length, inProfit, inLoss, netPnl: sumProfit + sumLoss };
}

function PosDirBlock({
  direction,
  positions,
}: {
  direction: "LONG" | "SHORT";
  positions: Position[];
}) {
  const s = calcPosSummary(positions);
  const isLong = direction === "LONG";
  const netPos = s.netPnl > 0;
  const netClass = netPos
    ? "text-emerald-400"
    : s.netPnl < 0
    ? "text-rose-400"
    : "text-slate-400";
  const borderColor = isLong ? "border-emerald-400/20" : "border-rose-400/20";
  const bgColor = isLong ? "bg-emerald-400/[0.04]" : "bg-rose-400/[0.04]";
  const badgeCls = isLong
    ? "border-emerald-400/40 bg-emerald-400/10 text-emerald-400"
    : "border-rose-400/40 bg-rose-400/10 text-rose-400";

  return (
    <div className={`rounded-2xl border ${borderColor} ${bgColor} p-4`}>
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <span className={`inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-[11px] font-bold uppercase tracking-widest ${badgeCls}`}>
            {isLong ? "↑" : "↓"} {direction}
          </span>
          <span className="text-sm text-slate-400">{s.count} поз.</span>
        </div>
        <div className={`font-mono font-bold tabular-nums text-lg ${netClass}`}>
          {s.netPnl >= 0 ? "+" : ""}{s.netPnl.toFixed(2)}%
        </div>
      </div>
      <div className="grid grid-cols-2 gap-2 border-t border-white/5 pt-3 text-xs">
        <div>
          <div className="text-[10px] uppercase tracking-wider text-slate-500 mb-0.5">Прибыльных</div>
          <div className="font-mono font-semibold text-emerald-400">{s.inProfit}</div>
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-wider text-slate-500 mb-0.5">Убыточных</div>
          <div className="font-mono font-semibold text-rose-400">{s.inLoss}</div>
        </div>
      </div>
    </div>
  );
}

function PositionsSummaryWidget() {
  const { data, isLoading } = usePositions();
  const longs  = data?.positions.filter((p) => p.direction === "LONG")  ?? [];
  const shorts = data?.positions.filter((p) => p.direction === "SHORT") ?? [];

  return (
    <section className="mt-4">
      <div className="rounded-2xl border border-card-border bg-card/40 p-4 backdrop-blur-sm">
        <div className="mb-3 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <span className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">
              Открытые позиции
            </span>
            {data && (
              <span className="rounded-full border border-card-border px-2 py-0.5 text-[10px] text-muted-foreground/60">
                {data.count} всего
              </span>
            )}
          </div>
          <Link
            href="/positions"
            className="text-[11px] text-primary/70 hover:text-primary transition-colors"
          >
            Все позиции →
          </Link>
        </div>

        {isLoading ? (
          <div className="grid grid-cols-2 gap-3">
            <div className="h-24 animate-pulse rounded-2xl bg-muted/30" />
            <div className="h-24 animate-pulse rounded-2xl bg-muted/30" />
          </div>
        ) : !data?.count ? (
          <div className="py-4 text-center text-sm text-muted-foreground">
            Нет открытых позиций
          </div>
        ) : (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            {longs.length  > 0 && <PosDirBlock direction="LONG"  positions={longs}  />}
            {shorts.length > 0 && <PosDirBlock direction="SHORT" positions={shorts} />}
          </div>
        )}
      </div>
    </section>
  );
}

function SkeletonRow() {
  return (
    <div className="rounded-xl border border-card-border bg-card/30 px-4 py-3">
      <div className="flex items-center gap-3">
        <div className="h-5 w-24 animate-pulse rounded bg-muted" />
        <div className="h-4 w-20 animate-pulse rounded bg-muted/70" />
      </div>
      <div className="mt-2 h-3 w-48 animate-pulse rounded bg-muted/60" />
    </div>
  );
}

export default function Dashboard() {
  const [filter, setFilter] = useState<SideFilter>("all");
  const signals = useSignals(filter === "all" ? undefined : filter);
  const stats = useStats();

  const seenIds = useRef<Set<number>>(new Set());
  const [newIds, setNewIds] = useState<Set<number>>(new Set());

  useEffect(() => {
    const list = signals.data?.signals ?? [];
    if (!list.length) return;
    const fresh = new Set<number>();
    let firstLoad = seenIds.current.size === 0;
    for (const s of list) {
      const idNum = typeof s.id === "string" ? parseInt(s.id, 10) : s.id;
      if (!seenIds.current.has(idNum)) {
        if (!firstLoad) fresh.add(idNum);
        seenIds.current.add(idNum);
      }
    }
    if (fresh.size) {
      setNewIds(fresh);
      const t = setTimeout(() => setNewIds(new Set()), 3000);
      return () => clearTimeout(t);
    }
    return undefined;
  }, [signals.data]);

  const sideMix = useMemo(() => {
    const rows = stats.data?.byTypeLast7d ?? [];
    let buy = 0;
    let sell = 0;
    for (const r of rows) {
      if (r.side === "buy") buy += r.count;
      else if (r.side === "sell") sell += r.count;
    }
    const total = buy + sell;
    const buyPct = total ? Math.round((buy / total) * 100) : 0;
    return { buy, sell, buyPct, sellPct: total ? 100 - buyPct : 0 };
  }, [stats.data]);

  const list = signals.data?.signals ?? [];

  return (
    <div className="min-h-screen bg-background">
      <div
        aria-hidden
        className="pointer-events-none fixed inset-x-0 top-0 h-[420px] bg-gradient-to-b from-primary/[0.08] via-primary/[0.02] to-transparent blur-2xl"
      />
      <div className="relative mx-auto w-full max-w-6xl px-4 pb-24 pt-8 sm:px-6 sm:pt-12">
        <header className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <div className="flex items-center gap-2 text-[11px] uppercase tracking-[0.28em] text-primary/80">
              <span className="inline-block h-px w-6 bg-primary/50" />
              Binance · 5m scan
            </div>
            <h1 className="mt-2 font-mono text-3xl font-semibold tracking-tight sm:text-4xl">
              Crypto<span className="text-primary"> Signals</span>
            </h1>
            <p className="mt-1 text-sm text-muted-foreground">
              Живая лента сигналов на покупку и продажу по USDT-парам.
            </p>
            <nav className="mt-3 flex items-center gap-1 text-sm">
              <span className="rounded-md bg-primary/10 px-3 py-1.5 font-medium text-primary">
                Лента
              </span>
              <Link
                href="/performance"
                className="rounded-md px-3 py-1.5 text-slate-400 hover:bg-white/5 hover:text-slate-200"
                data-testid="link-performance"
              >
                Производительность
              </Link>
              <Link
                href="/positions"
                className="rounded-md px-3 py-1.5 text-slate-400 hover:bg-white/5 hover:text-slate-200"
                data-testid="link-positions"
              >
                Позиции
              </Link>
            </nav>
          </div>
          <StatusPill />
        </header>

        <section className="mt-8 grid grid-cols-2 gap-3 sm:grid-cols-4">
          <KpiCard
            label="Всего сигналов"
            value={
              stats.isLoading
                ? "—"
                : (stats.data?.total ?? 0).toLocaleString("ru-RU")
            }
            hint="за всё время"
          />
          <KpiCard
            label="За 24 часа"
            value={
              stats.isLoading
                ? "—"
                : (stats.data?.last24h ?? 0).toLocaleString("ru-RU")
            }
            hint="свежие алерты"
          />
          <KpiCard
            label="Покупка / 7д"
            value={stats.isLoading ? "—" : sideMix.buy.toLocaleString("ru-RU")}
            hint={`${sideMix.buyPct}% от размеченных`}
            tone="buy"
          />
          <KpiCard
            label="Продажа / 7д"
            value={stats.isLoading ? "—" : sideMix.sell.toLocaleString("ru-RU")}
            hint={`${sideMix.sellPct}% от размеченных`}
            tone="sell"
          />
        </section>

        {/* Period breakdown */}
        <section className="mt-4">
          <div className="rounded-2xl border border-card-border bg-card/40 p-4 backdrop-blur-sm">
            <div className="mb-3 text-[11px] uppercase tracking-[0.18em] text-muted-foreground">
              Сигналов по периодам
            </div>
            {stats.isLoading ? (
              <div className="grid grid-cols-3 gap-2 sm:grid-cols-6">
                {Array.from({ length: 6 }).map((_, i) => (
                  <div key={i} className="h-12 animate-pulse rounded-lg bg-muted/40" />
                ))}
              </div>
            ) : (
              <div className="grid grid-cols-3 gap-2 sm:grid-cols-6">
                {(
                  [
                    { key: "18h",  label: "18 часов" },
                    { key: "24h",  label: "24 часа"  },
                    { key: "3d",   label: "3 дня"    },
                    { key: "6d",   label: "6 дней"   },
                    { key: "12d",  label: "12 дней"  },
                    { key: "30d",  label: "30 дней"  },
                  ] as const
                ).map(({ key, label }) => {
                  const cnt = stats.data?.periodCounts?.[key] ?? 0;
                  return (
                    <div
                      key={key}
                      className="flex flex-col items-center rounded-xl border border-card-border bg-card/60 px-2 py-3"
                    >
                      <span className="font-mono text-xl font-semibold tabular-nums leading-none">
                        {cnt.toLocaleString("ru-RU")}
                      </span>
                      <span className="mt-1.5 text-[10px] uppercase tracking-wider text-muted-foreground">
                        {label}
                      </span>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </section>

        <SignalStatsWidget />

        <PositionsSummaryWidget />

        <section className="mt-10">
          <div className="flex flex-wrap items-end justify-between gap-3">
            <div>
              <h2 className="font-mono text-lg font-semibold">Лента</h2>
              <p className="text-xs text-muted-foreground">
                Обновляется каждые 30 секунд.
              </p>
            </div>
            <div
              role="tablist"
              className="inline-flex rounded-full border border-border bg-card/60 p-1 backdrop-blur-sm"
            >
              {FILTERS.map((f) => {
                const active = filter === f.id;
                return (
                  <button
                    key={f.id}
                    role="tab"
                    aria-selected={active}
                    onClick={() => setFilter(f.id)}
                    data-testid={`filter-${f.id}`}
                    className={`relative rounded-full px-4 py-1.5 text-xs font-medium transition-colors ${
                      active
                        ? "bg-primary text-primary-foreground shadow-[0_0_18px_-4px_hsl(var(--primary)/0.6)]"
                        : "text-muted-foreground hover:text-foreground"
                    }`}
                  >
                    {f.label}
                  </button>
                );
              })}
            </div>
          </div>

          <div className="mt-4 space-y-2">
            {signals.isError ? (
              <div className="rounded-xl border border-destructive/30 bg-destructive/5 px-4 py-6 text-sm text-destructive">
                Не удалось загрузить ленту. Проверьте, что бот запущен.
              </div>
            ) : signals.isLoading ? (
              <>
                <SkeletonRow />
                <SkeletonRow />
                <SkeletonRow />
                <SkeletonRow />
              </>
            ) : list.length === 0 ? (
              <div className="rounded-2xl border border-card-border bg-card/40 px-6 py-16 text-center">
                <div className="font-mono text-sm uppercase tracking-[0.2em] text-primary/80">
                  Тишина в эфире
                </div>
                <p className="mt-2 text-sm text-muted-foreground">
                  Сигналов пока нет — бот их соберёт в следующем цикле.
                </p>
              </div>
            ) : (
              list.map((s) => {
                const idNum = typeof s.id === "string" ? parseInt(s.id, 10) : s.id;
                return (
                  <SignalRow
                    key={s.id}
                    s={s}
                    isNew={newIds.has(idNum)}
                  />
                );
              })
            )}
          </div>
        </section>

        <AiAnalyzeWidget />
        <AnalyticsWidget />

        <footer className="mt-16 border-t border-card-border pt-6 text-center text-[11px] uppercase tracking-[0.2em] text-muted-foreground">
          Сканирование каждые 5 минут · Источник: Binance
        </footer>
      </div>
    </div>
  );
}
