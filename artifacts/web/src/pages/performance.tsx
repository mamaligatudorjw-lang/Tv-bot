import { useMemo, useState } from "react";
import { Link } from "wouter";
import { usePerformance } from "@/hooks/use-api";
import type { Horizon, PerformanceBucket } from "@/types/api";

const WINDOWS = [
  { days: 7, label: "7 дней" },
  { days: 14, label: "14 дней" },
  { days: 30, label: "30 дней" },
  { days: 90, label: "90 дней" },
];

const HORIZONS: { key: Horizon; label: string }[] = [
  { key: "15m", label: "15 минут" },
  { key: "1h", label: "1 час" },
  { key: "4h", label: "4 часа" },
];

type SortKey = "count" | "winRate" | "avgReturn";

function formatPct(v: number | null, digits = 1): string {
  if (v === null || Number.isNaN(v)) return "—";
  return `${(v * 100).toFixed(digits)}%`;
}

function sideClass(side: "buy" | "sell") {
  return side === "buy"
    ? "text-emerald-400 border-emerald-400/40 bg-emerald-400/10"
    : "text-rose-400 border-rose-400/40 bg-rose-400/10";
}

function valueClass(v: number | null, goodIfPositive = true): string {
  if (v === null) return "text-slate-500";
  const positive = v > 0;
  const good = goodIfPositive ? positive : !positive;
  return good ? "text-emerald-400" : "text-rose-400";
}

function winRateClass(v: number | null): string {
  if (v === null) return "text-slate-500";
  if (v >= 0.6) return "text-emerald-400 font-semibold";
  if (v >= 0.5) return "text-emerald-300";
  if (v >= 0.4) return "text-amber-300";
  return "text-rose-400";
}

export default function Performance() {
  const [windowDays, setWindowDays] = useState(30);
  const [horizon, setHorizon] = useState<Horizon>("4h");
  const [sortKey, setSortKey] = useState<SortKey>("count");
  const { data, isLoading, error, isFetching } = usePerformance(windowDays);

  const sorted = useMemo(() => {
    if (!data?.byType) return [];
    const copy = [...data.byType];
    copy.sort((a, b) => {
      if (sortKey === "count") return b.count - a.count;
      const va = a.horizons[horizon][sortKey];
      const vb = b.horizons[horizon][sortKey];
      if (va === null && vb === null) return 0;
      if (va === null) return 1;
      if (vb === null) return -1;
      return vb - va;
    });
    return copy;
  }, [data, sortKey, horizon]);

  const summary = useMemo(() => {
    if (!data?.byType?.length) return null;
    let totalFollowups = 0;
    let totalWins = 0;
    let sumReturn = 0;
    for (const e of data.byType) {
      const h = e.horizons[horizon];
      totalFollowups += h.followups;
      totalWins += h.winRate !== null ? h.winRate * h.followups : 0;
      sumReturn += h.avgReturn !== null ? h.avgReturn * h.followups : 0;
    }
    return {
      totalFollowups,
      avgWinRate: totalFollowups ? totalWins / totalFollowups : null,
      avgReturn: totalFollowups ? sumReturn / totalFollowups : null,
    };
  }, [data, horizon]);

  return (
    <div className="min-h-screen bg-background text-foreground">
      <header className="sticky top-0 z-10 border-b border-white/5 bg-background/80 backdrop-blur">
        <div className="mx-auto flex max-w-6xl items-center justify-between gap-4 px-4 py-3">
          <div className="flex items-center gap-3">
            <h1 className="text-lg font-semibold tracking-tight">Crypto Signals</h1>
            <nav className="flex items-center gap-1 text-sm">
              <Link
                href="/"
                className="rounded-md px-3 py-1.5 text-slate-400 hover-elevate"
                data-testid="link-dashboard"
              >
                Лента
              </Link>
              <span className="rounded-md bg-primary/10 px-3 py-1.5 font-medium text-primary">
                Производительность
              </span>
            </nav>
          </div>
          {isFetching && (
            <span className="text-xs text-slate-500" data-testid="text-loading">
              обновление…
            </span>
          )}
        </div>
      </header>

      <main className="mx-auto max-w-6xl px-4 py-6">
        <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
          <div>
            <h2 className="text-2xl font-bold tracking-tight">Производительность сигналов</h2>
            <p className="mt-1 text-sm text-slate-400">
              Win-rate и средняя доходность по типам алертов — считается по фактическим
              ценам Binance после сигнала.
            </p>
          </div>
          <div className="flex flex-col gap-2 sm:flex-row">
            <div
              className="inline-flex rounded-md border border-white/10 bg-white/5 p-0.5"
              role="radiogroup"
              aria-label="Окно"
            >
              {WINDOWS.map((w) => (
                <button
                  key={w.days}
                  type="button"
                  role="radio"
                  aria-checked={windowDays === w.days}
                  onClick={() => setWindowDays(w.days)}
                  className={`rounded px-3 py-1.5 text-sm transition ${
                    windowDays === w.days
                      ? "bg-primary text-primary-foreground"
                      : "text-slate-300 hover:bg-white/5"
                  }`}
                  data-testid={`button-window-${w.days}`}
                >
                  {w.label}
                </button>
              ))}
            </div>
            <div
              className="inline-flex rounded-md border border-white/10 bg-white/5 p-0.5"
              role="radiogroup"
              aria-label="Горизонт"
            >
              {HORIZONS.map((h) => (
                <button
                  key={h.key}
                  type="button"
                  role="radio"
                  aria-checked={horizon === h.key}
                  onClick={() => setHorizon(h.key)}
                  className={`rounded px-3 py-1.5 text-sm transition ${
                    horizon === h.key
                      ? "bg-primary text-primary-foreground"
                      : "text-slate-300 hover:bg-white/5"
                  }`}
                  data-testid={`button-horizon-${h.key}`}
                >
                  {h.label}
                </button>
              ))}
            </div>
          </div>
        </div>

        {summary && (
          <div className="mb-6 grid gap-3 sm:grid-cols-3">
            <SummaryCard
              label="Сигналов с фоллоу-апом"
              value={`${summary.totalFollowups}`}
              hint={`из ${data?.totalSignals ?? 0} всего за ${windowDays} дн.`}
              testId="kpi-followups"
            />
            <SummaryCard
              label={`Средний win-rate (${horizon})`}
              value={formatPct(summary.avgWinRate)}
              valueClass={winRateClass(summary.avgWinRate)}
              testId="kpi-winrate"
            />
            <SummaryCard
              label={`Средняя доходность (${horizon})`}
              value={formatPct(summary.avgReturn, 2)}
              valueClass={valueClass(summary.avgReturn)}
              testId="kpi-return"
            />
          </div>
        )}

        {error && (
          <div
            className="rounded-md border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-300"
            data-testid="text-error"
          >
            Не удалось загрузить статистику. Попробуйте обновить страницу.
          </div>
        )}

        {isLoading && !error && (
          <div className="space-y-2">
            {Array.from({ length: 6 }).map((_, i) => (
              <div
                key={i}
                className="h-14 animate-pulse rounded-md border border-white/5 bg-white/[0.03]"
              />
            ))}
          </div>
        )}

        {!isLoading && !error && sorted.length === 0 && (
          <div
            className="rounded-md border border-white/10 bg-white/[0.03] px-4 py-8 text-center text-sm text-slate-400"
            data-testid="text-empty"
          >
            Пока нет настолько старых сигналов, чтобы посчитать результат. Дайте боту
            поработать ещё немного.
          </div>
        )}

        {!isLoading && !error && sorted.length > 0 && (
          <div className="overflow-hidden rounded-lg border border-white/10 bg-white/[0.02]">
            <table className="w-full text-sm" data-testid="table-performance">
              <thead className="bg-white/[0.03] text-xs uppercase tracking-wide text-slate-400">
                <tr>
                  <th className="px-4 py-3 text-left font-medium">Тип сигнала</th>
                  <th className="px-3 py-3 text-left font-medium">Сторона</th>
                  <SortableTh
                    label="Всего"
                    active={sortKey === "count"}
                    onClick={() => setSortKey("count")}
                    testId="th-sort-count"
                  />
                  <th className="px-3 py-3 text-right font-medium">Фоллоу-ап</th>
                  <SortableTh
                    label={`Win-rate (${horizon})`}
                    active={sortKey === "winRate"}
                    onClick={() => setSortKey("winRate")}
                    testId="th-sort-winrate"
                  />
                  <SortableTh
                    label={`Средняя (${horizon})`}
                    active={sortKey === "avgReturn"}
                    onClick={() => setSortKey("avgReturn")}
                    testId="th-sort-return"
                  />
                </tr>
              </thead>
              <tbody>
                {sorted.map((b) => (
                  <PerformanceRow key={`${b.alertType}-${b.side}`} bucket={b} horizon={horizon} />
                ))}
              </tbody>
            </table>
          </div>
        )}

        <p className="mt-4 text-xs text-slate-500">
          «Доходность» считается в сторону сигнала: для покупки — рост цены, для продажи —
          падение. Учитываются только сигналы старше 4 часов, чтобы успел заполниться
          фоллоу-ап. Это не финансовая рекомендация.
        </p>
      </main>
    </div>
  );
}

function SummaryCard({
  label,
  value,
  hint,
  valueClass: vClass,
  testId,
}: {
  label: string;
  value: string;
  hint?: string;
  valueClass?: string;
  testId?: string;
}) {
  return (
    <div
      className="rounded-lg border border-white/10 bg-white/[0.03] px-4 py-3"
      data-testid={testId}
    >
      <div className="text-xs uppercase tracking-wide text-slate-400">{label}</div>
      <div className={`mt-1 text-2xl font-semibold tabular-nums ${vClass ?? ""}`}>{value}</div>
      {hint && <div className="mt-0.5 text-xs text-slate-500">{hint}</div>}
    </div>
  );
}

function SortableTh({
  label,
  active,
  onClick,
  testId,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
  testId?: string;
}) {
  return (
    <th className="px-3 py-3 text-right font-medium">
      <button
        type="button"
        onClick={onClick}
        className={`inline-flex items-center gap-1 transition ${
          active ? "text-primary" : "text-slate-400 hover:text-slate-200"
        }`}
        data-testid={testId}
      >
        {label}
        <span aria-hidden>{active ? "↓" : ""}</span>
      </button>
    </th>
  );
}

function PerformanceRow({ bucket, horizon }: { bucket: PerformanceBucket; horizon: Horizon }) {
  const h = bucket.horizons[horizon];
  return (
    <tr
      className="border-t border-white/5 hover:bg-white/[0.02]"
      data-testid={`row-${bucket.alertType}-${bucket.side}`}
    >
      <td className="px-4 py-3 font-medium">{bucket.label}</td>
      <td className="px-3 py-3">
        <span
          className={`inline-flex rounded-md border px-2 py-0.5 text-xs uppercase tracking-wide ${sideClass(
            bucket.side,
          )}`}
        >
          {bucket.side === "buy" ? "Покупка" : "Продажа"}
        </span>
      </td>
      <td className="px-3 py-3 text-right tabular-nums text-slate-200">{bucket.count}</td>
      <td className="px-3 py-3 text-right tabular-nums text-slate-400">{h.followups}</td>
      <td className={`px-3 py-3 text-right tabular-nums ${winRateClass(h.winRate)}`}>
        {formatPct(h.winRate)}
      </td>
      <td className={`px-3 py-3 text-right tabular-nums ${valueClass(h.avgReturn)}`}>
        {formatPct(h.avgReturn, 2)}
      </td>
    </tr>
  );
}
