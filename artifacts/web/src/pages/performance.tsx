import { useMemo, useState } from "react";
import { Link } from "wouter";
import { usePerformance } from "@/hooks/use-api";
import type { Horizon, PerformanceBucket } from "@/types/api";

const WINDOWS = [
  { key: "18h", label: "18ч"    },
  { key: "24h", label: "24ч"    },
  { key: "3d",  label: "3 дня"  },
  { key: "6d",  label: "6 дней" },
  { key: "12d", label: "12 дней"},
  { key: "30d", label: "30 дней"},
];

const HORIZONS: { key: Horizon; label: string }[] = [
  { key: "3m",  label: "3м"  },
  { key: "5m",  label: "5м"  },
  { key: "15m", label: "15м" },
  { key: "1h",  label: "1ч"  },
  { key: "4h",  label: "4ч"  },
];

/** Sort by win-rate for a horizon, or by count. */
type SortKey = "count" | Horizon;

function formatPct(v: number | null, digits = 1): string {
  if (v === null || Number.isNaN(v)) return "—";
  return `${(v * 100).toFixed(digits)}%`;
}

function sideClass(side: "buy" | "sell") {
  return side === "buy"
    ? "text-emerald-400 border-emerald-400/40 bg-emerald-400/10"
    : "text-rose-400 border-rose-400/40 bg-rose-400/10";
}

function winRateClass(v: number | null): string {
  if (v === null) return "text-slate-500";
  if (v >= 0.6)  return "text-emerald-400 font-semibold";
  if (v >= 0.5)  return "text-emerald-300";
  if (v >= 0.4)  return "text-amber-300";
  return "text-rose-400";
}

function returnClass(v: number | null): string {
  if (v === null) return "text-slate-500";
  return v > 0 ? "text-emerald-400" : "text-rose-400";
}

export default function Performance() {
  const [window, setWindow] = useState("30d");
  const [sortKey, setSortKey]       = useState<SortKey>("count");
  const { data, isLoading, error, isFetching } = usePerformance(window);

  const sorted = useMemo(() => {
    if (!data?.byType) return [];
    const copy = [...data.byType];
    copy.sort((a, b) => {
      if (sortKey === "count") return b.count - a.count;
      const ha = a.horizons[sortKey];
      const hb = b.horizons[sortKey];
      const va = ha?.winRate ?? null;
      const vb = hb?.winRate ?? null;
      if (va === null && vb === null) return 0;
      if (va === null) return 1;
      if (vb === null) return -1;
      return vb - va;
    });
    return copy;
  }, [data, sortKey]);

  /** KPI summary uses the 4h horizon as the "canonical" quality metric */
  const summary = useMemo(() => {
    if (!data?.byType?.length) return null;
    let totalFollowups = 0, totalWins = 0, sumReturn = 0;
    for (const e of data.byType) {
      const h = e.horizons["4h"];
      if (!h) continue;
      totalFollowups += h.followups;
      totalWins      += h.winRate  !== null ? h.winRate  * h.followups : 0;
      sumReturn      += h.avgReturn !== null ? h.avgReturn * h.followups : 0;
    }
    return {
      totalFollowups,
      avgWinRate: totalFollowups ? totalWins  / totalFollowups : null,
      avgReturn:  totalFollowups ? sumReturn  / totalFollowups : null,
    };
  }, [data]);

  return (
    <div className="min-h-screen bg-background text-foreground">
      {/* ── header ──────────────────────────────────────────────────── */}
      <header className="sticky top-0 z-10 border-b border-white/5 bg-background/80 backdrop-blur">
        <div className="mx-auto flex max-w-7xl items-center justify-between gap-4 px-4 py-3">
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
              <Link
                href="/positions"
                className="rounded-md px-3 py-1.5 text-slate-400 hover-elevate"
                data-testid="link-positions"
              >
                Позиции
              </Link>
            </nav>
          </div>
          {isFetching && (
            <span className="text-xs text-slate-500" data-testid="text-loading">
              обновление…
            </span>
          )}
        </div>
      </header>

      <main className="mx-auto max-w-7xl px-4 py-6">
        {/* ── controls ────────────────────────────────────────────────── */}
        <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
          <div>
            <h2 className="text-2xl font-bold tracking-tight">
              Производительность сигналов
            </h2>
            <p className="mt-1 text-sm text-slate-400">
              Win-rate и средняя доходность по всем горизонтам — по фактическим ценам Binance.
            </p>
          </div>
          <div
            className="inline-flex rounded-md border border-white/10 bg-white/5 p-0.5"
            role="radiogroup"
            aria-label="Окно"
          >
            {WINDOWS.map((w) => (
              <button
                key={w.key}
                type="button"
                role="radio"
                aria-checked={window === w.key}
                onClick={() => setWindow(w.key)}
                className={`rounded px-3 py-1.5 text-sm transition ${
                  window === w.key
                    ? "bg-primary text-primary-foreground"
                    : "text-slate-300 hover:bg-white/5"
                }`}
                data-testid={`button-window-${w.key}`}
              >
                {w.label}
              </button>
            ))}
          </div>
        </div>

        {/* ── KPI cards (4h summary) ───────────────────────────────────── */}
        {summary && (
          <div className="mb-6 grid gap-3 sm:grid-cols-3">
            <SummaryCard
              label="Данных 4ч"
              value={`${summary.totalFollowups}`}
              hint={`из ${data?.totalSignals ?? 0} всего за ${data?.window ?? window}`}
              testId="kpi-followups"
            />
            <SummaryCard
              label="Средний win-rate (4ч)"
              value={formatPct(summary.avgWinRate)}
              valueClass={winRateClass(summary.avgWinRate)}
              testId="kpi-winrate"
            />
            <SummaryCard
              label="Средняя доходность (4ч)"
              value={formatPct(summary.avgReturn, 2)}
              valueClass={returnClass(summary.avgReturn)}
              testId="kpi-return"
            />
          </div>
        )}

        {/* ── states ──────────────────────────────────────────────────── */}
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
                className="h-16 animate-pulse rounded-md border border-white/5 bg-white/[0.03]"
              />
            ))}
          </div>
        )}
        {!isLoading && !error && sorted.length === 0 && (
          <div
            className="rounded-md border border-white/10 bg-white/[0.03] px-4 py-8 text-center text-sm text-slate-400"
            data-testid="text-empty"
          >
            Пока нет достаточно старых сигналов. Дайте боту поработать.
          </div>
        )}

        {/* ── main table ──────────────────────────────────────────────── */}
        {!isLoading && !error && sorted.length > 0 && (
          <div className="overflow-x-auto rounded-lg border border-white/10 bg-white/[0.02]">
            <table
              className="w-full text-sm"
              data-testid="table-performance"
              style={{ minWidth: 820 }}
            >
              <thead className="bg-white/[0.03] text-xs uppercase tracking-wide text-slate-400">
                <tr>
                  <th className="px-4 py-3 text-left font-medium">Тип сигнала</th>
                  <th className="px-3 py-3 text-left font-medium">Сторона</th>

                  {/* count — sortable */}
                  <th className="px-3 py-3 text-right font-medium">
                    <button
                      type="button"
                      onClick={() => setSortKey("count")}
                      className={`inline-flex items-center gap-1 transition ${
                        sortKey === "count" ? "text-primary" : "text-slate-400 hover:text-slate-200"
                      }`}
                      data-testid="th-sort-count"
                    >
                      Всего{sortKey === "count" ? " ↓" : ""}
                    </button>
                  </th>

                  {/* one column per horizon — click to sort by win-rate */}
                  {HORIZONS.map((h) => (
                    <th key={h.key} className="px-2 py-3 text-center font-medium">
                      <button
                        type="button"
                        onClick={() => setSortKey(h.key)}
                        className={`inline-flex flex-col items-center gap-0.5 transition ${
                          sortKey === h.key
                            ? "text-primary"
                            : "text-slate-400 hover:text-slate-200"
                        }`}
                        data-testid={`th-sort-${h.key}`}
                      >
                        <span>{h.label}{sortKey === h.key ? " ↓" : ""}</span>
                        <span className="text-[10px] font-normal normal-case text-slate-500">
                          W% / avg
                        </span>
                      </button>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {sorted.map((b) => (
                  <PerformanceRow
                    key={`${b.alertType}-${b.side}`}
                    bucket={b}
                    activeHorizon={sortKey !== "count" ? sortKey : null}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}

        <p className="mt-4 text-xs text-slate-500">
          Win-rate в сторону сигнала (покупка — рост, продажа — падение). Цены 3м/5м снимаются на
          следующем 5-минутном тике (≈ 3–10 мин после сигнала — известная погрешность архитектуры).
          В скобках — количество наблюдений. Не является финансовой рекомендацией.
        </p>
      </main>
    </div>
  );
}

/* ── sub-components ──────────────────────────────────────────────────────── */

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

function HorizonCell({
  stat,
  isActive,
}: {
  stat: { followups: number; winRate: number | null; avgReturn: number | null } | undefined;
  isActive: boolean;
}) {
  if (!stat || stat.followups === 0) {
    return (
      <td className={`px-2 py-2 text-center tabular-nums ${isActive ? "bg-primary/5" : ""}`}>
        <span className="text-slate-600">—</span>
      </td>
    );
  }

  const { winRate: wr, avgReturn: ar, followups } = stat;
  const arSign = ar !== null && ar >= 0 ? "+" : "";

  return (
    <td
      className={`px-2 py-2 text-center tabular-nums ${isActive ? "bg-primary/5 rounded" : ""}`}
    >
      {/* win-rate */}
      <div className={`text-sm leading-tight ${winRateClass(wr)}`}>
        {wr !== null ? `${(wr * 100).toFixed(0)}%` : "—"}
      </div>
      {/* avg return */}
      <div className={`text-xs leading-tight ${returnClass(ar)}`}>
        {ar !== null ? `${arSign}${(ar * 100).toFixed(2)}%` : "—"}
      </div>
      {/* follow-up count */}
      <div className="text-[10px] leading-tight text-slate-600">({followups})</div>
    </td>
  );
}

function PerformanceRow({
  bucket,
  activeHorizon,
}: {
  bucket: PerformanceBucket;
  activeHorizon: Horizon | null;
}) {
  return (
    <tr
      className="border-t border-white/5 hover:bg-white/[0.02]"
      data-testid={`row-${bucket.alertType}-${bucket.side}`}
    >
      <td className="px-4 py-2 font-medium">{bucket.label}</td>
      <td className="px-3 py-2">
        <span
          className={`inline-flex rounded-md border px-2 py-0.5 text-xs uppercase tracking-wide ${sideClass(
            bucket.side,
          )}`}
        >
          {bucket.side === "buy" ? "Покупка" : "Продажа"}
        </span>
      </td>
      <td className="px-3 py-2 text-right tabular-nums text-slate-200">{bucket.count}</td>
      {HORIZONS.map((h) => (
        <HorizonCell
          key={h.key}
          stat={bucket.horizons[h.key]}
          isActive={activeHorizon === h.key}
        />
      ))}
    </tr>
  );
}
