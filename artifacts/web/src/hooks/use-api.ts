import { useQuery } from "@tanstack/react-query";
import type {
  AnalyticsResponse,
  BotStatus,
  PerformanceResponse,
  PositionsResponse,
  SignalsResponse,
  StatsResponse,
} from "../types/api";

const API_BASE = "/bot-api";

/** Fire-and-forget page-view beacon. Never throws. */
export function trackPageView(page: string): void {
  try {
    const body = JSON.stringify({ page });
    if (typeof navigator !== "undefined" && navigator.sendBeacon) {
      navigator.sendBeacon(`${API_BASE}/track`, new Blob([body], { type: "application/json" }));
    } else {
      fetch(`${API_BASE}/track`, { method: "POST", headers: { "Content-Type": "application/json" }, body, keepalive: true }).catch(() => {});
    }
  } catch { /* ignore */ }
}

export function useBotStatus() {
  return useQuery<BotStatus>({
    queryKey: ["botStatus"],
    queryFn: async () => {
      const res = await fetch(`${API_BASE}/status`);
      if (!res.ok) throw new Error("Failed to fetch bot status");
      return res.json();
    },
    refetchInterval: 30000, // 30s
  });
}

export function useSignals(side?: "buy" | "sell" | "neutral") {
  return useQuery<SignalsResponse>({
    queryKey: ["signals", side],
    queryFn: async () => {
      const url = new URL(`${API_BASE}/signals/recent`, window.location.origin);
      url.searchParams.set("limit", "50");
      if (side && side !== "neutral") {
        url.searchParams.set("side", side);
      }
      const res = await fetch(url.toString());
      if (!res.ok) throw new Error("Failed to fetch signals");
      return res.json();
    },
    refetchInterval: 30000, // 30s
  });
}

export function useStats() {
  return useQuery<StatsResponse>({
    queryKey: ["stats"],
    queryFn: async () => {
      const res = await fetch(`${API_BASE}/signals/stats`);
      if (!res.ok) throw new Error("Failed to fetch stats");
      return res.json();
    },
    refetchInterval: 120000, // 2m
  });
}

export function usePositions() {
  return useQuery<PositionsResponse>({
    queryKey: ["positions"],
    queryFn: async () => {
      const res = await fetch(`${API_BASE}/positions`);
      if (!res.ok) throw new Error("Failed to fetch positions");
      return res.json();
    },
    refetchInterval: 15_000, // 15s — live PnL
  });
}

export function useAnalytics() {
  return useQuery<AnalyticsResponse>({
    queryKey: ["analytics"],
    queryFn: async () => {
      const res = await fetch(`${API_BASE}/analytics`);
      if (!res.ok) throw new Error("Failed to fetch analytics");
      return res.json();
    },
    refetchInterval: 60_000, // 1m
  });
}

export function usePerformance(window: string) {
  return useQuery<PerformanceResponse>({
    queryKey: ["performance", window],
    queryFn: async () => {
      const url = new URL(`${API_BASE}/signals/performance`, globalThis.location.origin);
      url.searchParams.set("window", window);
      const res = await fetch(url.toString());
      if (!res.ok) throw new Error("Failed to fetch performance");
      return res.json();
    },
    refetchInterval: 5 * 60_000, // 5m — backtest changes slowly
  });
}
