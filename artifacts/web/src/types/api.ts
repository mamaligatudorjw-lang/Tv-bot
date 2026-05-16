export interface BotStatus {
  initialized: boolean;
  trackedPairs: number;
  silenced: boolean;
  lastRun: number | null;
  lastRunSummary: string | null;
  activeBinanceHost: string;
}

export interface Signal {
  id: string;
  ts: number;
  symbol: string;
  alertType: string;
  alertTypeLabel: string;
  recommendation: string | null;
  side: "buy" | "sell" | "neutral";
  priceAtAlert: number;
  price15m: number | null;
  price1h: number | null;
  price4h: number | null;
}

export interface SignalsResponse {
  count: number;
  signals: Signal[];
}

export interface StatsResponse {
  total: number;
  last24h: number;
  byTypeLast7d: {
    alertType: string;
    label: string;
    count: number;
    side: "buy" | "sell" | "neutral";
  }[];
}

export type Horizon = "15m" | "1h" | "4h";

export interface HorizonStat {
  followups: number;
  winRate: number | null;
  avgReturn: number | null;
}

export interface PerformanceBucket {
  alertType: string;
  label: string;
  side: "buy" | "sell";
  count: number;
  horizons: Record<Horizon, HorizonStat>;
}

export interface PerformanceResponse {
  windowDays: number;
  since: number;
  until: number;
  totalSignals: number;
  byType: PerformanceBucket[];
}
