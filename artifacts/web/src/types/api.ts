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
  price3m:  number | null;
  price5m:  number | null;
  price15m: number | null;
  price1h:  number | null;
  price4h:  number | null;
  score: number | null;
}

export interface SignalsResponse {
  count: number;
  signals: Signal[];
}

export interface StatsResponse {
  total: number;
  last24h: number;
  periodCounts: {
    "18h": number;
    "24h": number;
    "3d":  number;
    "6d":  number;
    "12d": number;
    "30d": number;
  };
  byTypeLast7d: {
    alertType: string;
    label: string;
    count: number;
    side: "buy" | "sell" | "neutral";
  }[];
}

export type Horizon = "3m" | "5m" | "15m" | "1h" | "4h" | "1d" | "2d" | "3d" | "7d";

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

export interface Position {
  id: number;
  symbol: string;
  direction: "LONG" | "SHORT";
  entry: number;
  currentPrice: number | null;
  slPrice: number;
  tpPrice: number;
  pnlPct: number | null;
  tsOpen: number;
  elapsedSeconds: number;
}

export interface PositionsResponse {
  count: number;
  positions: Position[];
}

export interface PerformanceResponse {
  window: string;
  windowSeconds: number;
  since: number;
  until: number;
  totalSignals: number;
  byType: PerformanceBucket[];
}

export interface AnalyticsPeriod {
  views: number;
  unique: number;
  returning: number;
  returnRate: number;
}

export interface AnalyticsResponse {
  "1d":  AnalyticsPeriod;
  "7d":  AnalyticsPeriod;
  "30d": AnalyticsPeriod;
  pages: { page: string; views: number }[];
  daily: { date: string; views: number; unique: number }[];
}
