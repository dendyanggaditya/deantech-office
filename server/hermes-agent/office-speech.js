/**
 * Subscriber for agent turns published by the office bridge plugin.
 *
 * A hermes-agent backend delivers message events only to the client that
 * submitted the prompt, so an office watching from the outside never sees the
 * chatter when you talk to your agents somewhere else — the desktop app, the
 * TUI, the CLI. The companion plugin in `plugins/hermes3d-office-bridge`
 * republishes each finished turn on the backend's event bus; this reads that
 * bus and hands the turns to the bridge.
 *
 * The subscription is entirely optional. A backend without the plugin simply
 * never publishes, so the office behaves exactly as it did before. Connection
 * failures retry quietly rather than disturbing the gateway session.
 */

const { WebSocket } = require("ws");

/** Where the dashboard fans published frames out to subscribers. */
const HERMES_AGENT_EVENTS_PATH = "/api/events";

/** Must match the plugin's default and the backend's channel-name rules. */
const DEFAULT_CHANNEL = "hermes3d";

const RECONNECT_BACKOFF_MS = [1_000, 2_000, 5_000, 15_000, 30_000];

/** Frame shape the plugin emits; anything else on the channel is ignored. */
const FRAME_KIND = "agent.turn";

/** Turn-activity frames: an agent started or finished a turn. */
const ACTIVITY_PHASE_BY_KIND = {
  "agent.turn.start": "start",
  "agent.turn.end": "end",
};

/**
 * A turn older than this is dropped. Reconnecting can deliver a backlog, and
 * replaying stale speech would huddle idle characters for no reason.
 */
const MAX_TURN_AGE_MS = 20_000;

/** Same loopback-Host fallback the JSON-RPC client uses; see its rationale. */
const LOOPBACK_HOST_HEADER = "localhost";

const buildEventsUrl = (baseUrl, token, channel) => {
  const raw = typeof baseUrl === "string" ? baseUrl.trim() : "";
  if (!raw) throw new Error("hermes-agent URL is empty.");

  const parsed = new URL(raw);
  if (parsed.protocol === "https:") parsed.protocol = "wss:";
  else if (parsed.protocol === "http:") parsed.protocol = "ws:";
  else if (parsed.protocol !== "ws:" && parsed.protocol !== "wss:") {
    throw new Error(`Unsupported scheme "${parsed.protocol}" for a hermes-agent URL.`);
  }

  parsed.pathname = HERMES_AGENT_EVENTS_PATH;
  parsed.searchParams.set("channel", channel || DEFAULT_CHANNEL);
  if (typeof token === "string" && token.trim()) {
    parsed.searchParams.set("token", token.trim());
  }
  return parsed.toString();
};

const redactUrl = (url) => String(url).replace(/([?&]token=)[^&]*/gi, "$1***");

/**
 * Validate one published frame.
 *
 * Returns null for anything that is not a well-formed, recent turn: the
 * channel is shared, so unrelated or malformed traffic must not reach the
 * office.
 */
const parseTurnFrame = (raw, nowMs = Date.now()) => {
  const frame = decodeFrame(raw);
  if (!frame || frame.kind !== FRAME_KIND) return null;

  const profile = typeof frame.profile === "string" ? frame.profile.trim() : "";
  const text = typeof frame.text === "string" ? frame.text.trim() : "";
  if (!profile || !text) return null;

  const atMs = Number.isFinite(frame.atMs) ? Number(frame.atMs) : nowMs;
  if (nowMs - atMs > MAX_TURN_AGE_MS) return null;

  return {
    profile,
    text,
    sessionId: typeof frame.sessionId === "string" ? frame.sessionId : "",
    atMs,
  };
};

/**
 * Validate one turn-activity frame (`agent.turn.start` / `agent.turn.end`).
 *
 * Returns `{ phase, profile, sessionId, atMs }` or null. A stale start is
 * dropped like a stale turn — it would sit an agent down for work that already
 * finished — but an end is honoured however late it arrives, because the only
 * thing it can do is let an agent get up.
 */
const parseActivityFrame = (raw, nowMs = Date.now()) => {
  const frame = decodeFrame(raw);
  if (!frame) return null;
  const phase = ACTIVITY_PHASE_BY_KIND[frame.kind];
  if (!phase) return null;

  const profile = typeof frame.profile === "string" ? frame.profile.trim() : "";
  if (!profile) return null;

  const atMs = Number.isFinite(frame.atMs) ? Number(frame.atMs) : nowMs;
  if (phase === "start" && nowMs - atMs > MAX_TURN_AGE_MS) return null;

  return {
    phase,
    profile,
    sessionId: typeof frame.sessionId === "string" ? frame.sessionId : "",
    atMs,
  };
};

const decodeFrame = (raw) => {
  let frame;
  try {
    frame = JSON.parse(typeof raw === "string" ? raw : String(raw));
  } catch {
    return null;
  }
  return frame && typeof frame === "object" ? frame : null;
};

/**
 * Subscribe to published turns.
 *
 * `onTurn` is called with `{ profile, text, sessionId, atMs }` and
 * `onActivity` with `{ phase, profile, sessionId, atMs }`. Returns a handle
 * with `close()`.
 */
function createOfficeSpeechSubscriber(options) {
  const {
    url,
    token,
    channel = DEFAULT_CHANNEL,
    onTurn,
    onActivity,
    hostHeader = "",
    loopbackHostFallback = true,
    log = () => {},
  } = options || {};

  let eventsUrl;
  try {
    eventsUrl = buildEventsUrl(url, token, channel);
  } catch (err) {
    log(`[office-speech] disabled: ${err.message}`);
    return { close() {} };
  }

  let socket = null;
  let closed = false;
  let failures = 0;
  let timer = null;
  let triedLoopbackHost = false;

  const scheduleReconnect = () => {
    if (closed || timer) return;
    const gap = RECONNECT_BACKOFF_MS[Math.min(failures, RECONNECT_BACKOFF_MS.length - 1)];
    failures += 1;
    timer = setTimeout(() => {
      timer = null;
      open(triedLoopbackHost ? LOOPBACK_HOST_HEADER : hostHeader);
    }, gap);
    if (typeof timer.unref === "function") timer.unref();
  };

  function open(host) {
    if (closed) return;
    const wsOptions = {};
    if (host) wsOptions.headers = { Host: host };

    const ws = new WebSocket(eventsUrl, wsOptions);
    socket = ws;

    ws.on("open", () => {
      failures = 0;
      log(`[office-speech] subscribed to ${redactUrl(eventsUrl)}`);
    });

    ws.on("message", (raw) => {
      const asText = Buffer.isBuffer(raw) ? raw.toString() : raw;
      const turn = parseTurnFrame(asText);
      const activity = turn ? null : parseActivityFrame(asText);
      if (!turn && !activity) {
        log("[office-speech] frame rejected by parser");
        return;
      }
      try {
        if (turn) onTurn?.(turn);
        else onActivity?.(activity);
      } catch (err) {
        log(`[office-speech] handler failed: ${err.message}`);
      }
    });

    ws.on("close", (code) => {
      if (closed) return;
      // A loopback-bound backend refuses a foreign Host; retry once the way
      // the JSON-RPC client does before falling back to plain reconnects.
      if (code === 4403 && loopbackHostFallback && !host && !triedLoopbackHost) {
        triedLoopbackHost = true;
        open(LOOPBACK_HOST_HEADER);
        return;
      }
      scheduleReconnect();
    });

    ws.on("error", () => {
      // `close` always follows, which is where reconnects are scheduled.
    });
  }

  open(hostHeader);

  return {
    close() {
      closed = true;
      if (timer) clearTimeout(timer);
      timer = null;
      try {
        socket?.close();
      } catch {
        // Already gone.
      }
    },
  };
}

module.exports = {
  DEFAULT_CHANNEL,
  HERMES_AGENT_EVENTS_PATH,
  MAX_TURN_AGE_MS,
  buildEventsUrl,
  createOfficeSpeechSubscriber,
  parseActivityFrame,
  parseTurnFrame,
};
