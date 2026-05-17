import express, { type Express } from "express";
import cors from "cors";
import pinoHttp from "pino-http";
import router from "./routes";
import { logger } from "./lib/logger";

const app: Express = express();

app.use(
  pinoHttp({
    logger,
    serializers: {
      req(req) {
        return {
          id: req.id,
          method: req.method,
          url: req.url?.split("?")[0],
        };
      },
      res(res) {
        return {
          statusCode: res.statusCode,
        };
      },
    },
  }),
);
app.use(cors());

// Reverse-proxy /bot-api/* to the legacy Python Telegram bot (Flask on
// :5000). The bot lives outside the artifacts/ tree and isn't registered
// as its own routed artifact, so the global proxy doesn't know how to
// reach it. We claim the /bot-api path on this artifact and forward.
// Forward BEFORE express.json() so request bodies stream through raw —
// the bot exposes GET-only signal endpoints today, but this keeps the
// proxy correct if POST/PUT are added later.
const BOT_ORIGIN = process.env["BOT_ORIGIN"] ?? "http://127.0.0.1:5000";
app.use("/bot-api", async (req, res) => {
  const upstream = `${BOT_ORIGIN}/bot-api${req.url}`;
  try {
    const headers: Record<string, string> = {};
    for (const [k, v] of Object.entries(req.headers)) {
      if (v == null) continue;
      const key = k.toLowerCase();
      if (key === "host" || key === "connection" || key === "content-length") continue;
      headers[k] = Array.isArray(v) ? v.join(",") : String(v);
    }
    const init: RequestInit & { duplex?: "half" } = {
      method: req.method,
      headers,
    };
    if (!["GET", "HEAD"].includes(req.method)) {
      init.body = req as unknown as ReadableStream;
      init.duplex = "half";
    }
    const upstreamRes = await fetch(upstream, init);
    res.status(upstreamRes.status);
    upstreamRes.headers.forEach((value, key) => {
      const k = key.toLowerCase();
      if (k === "transfer-encoding" || k === "content-encoding" || k === "connection") return;
      res.setHeader(key, value);
    });
    const buf = Buffer.from(await upstreamRes.arrayBuffer());
    res.end(buf);
  } catch (err) {
    req.log.warn({ err, upstream }, "bot-api proxy failed");
    res.status(502).json({ error: "bot_unavailable" });
  }
});

app.use(express.json());
app.use(express.urlencoded({ extended: true }));

app.use("/api", router);

export default app;
