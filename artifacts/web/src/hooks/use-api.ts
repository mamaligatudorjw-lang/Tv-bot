import { useQuery } from "@tanstack/react-query";
import type {
  BotStatus,
  PerformanceResponse,
  PositionsResponse,
  SignalsResponse,
  StatsResponse,
} from "../types/api";

const API_BASE = "/bot-api";

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
