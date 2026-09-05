import { Router, type IRouter } from "express";
import { HealthCheckResponse } from "@workspace/api-zod";

const router: IRouter = Router();
const BOT_ORIGIN = process.env["BOT_ORIGIN"] ?? "http://127.0.0.1:5000";
const FLASK_PROBE_TIMEOUT_MS = 1_000;

const flaskStarting = {
  status: "starting",
  api: "ready",
  flask: "starting",
} as const;

router.get("/healthz", (_req, res) => {
  const data = HealthCheckResponse.parse({ status: "ok" });
  res.json(data);
});

router.get("/readiness", async (_req, res) => {
  const controller = new AbortController();
  const timeout = setTimeout(
    () => controller.abort(),
    FLASK_PROBE_TIMEOUT_MS,
  );

  try {
    const response = await fetch(`${BOT_ORIGIN}/ping`, {
      signal: controller.signal,
    });

    if (!response.ok) {
      res.status(503).json(flaskStarting);
      return;
    }

    res.json({
      status: "ready",
      api: "ready",
      flask: "ready",
    });
  } catch {
    // Connection refused, timeout/abort, and every other network exception
    // mean Flask is still starting from the readiness endpoint's perspective.
    res.status(503).json(flaskStarting);
  } finally {
    clearTimeout(timeout);
  }
});

export default router;
