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
