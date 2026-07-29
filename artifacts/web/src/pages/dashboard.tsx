import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "wouter";
import { useBotStatus, useSignals, useStats } from "@/hooks/use-api";
import type { Signal } from "@/types/api";
import {
  calcDeltaPercent,
  formatDelta,
  formatPrice,
  formatSymbol,
  formatTimeAgo,
} from "@/lib/format";

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

        <footer className="mt-16 border-t border-card-border pt-6 text-center text-[11px] uppercase tracking-[0.2em] text-muted-foreground">
          Сканирование каждые 5 минут · Источник: Binance
        </footer>
      </div>
    </div>
  );
}
