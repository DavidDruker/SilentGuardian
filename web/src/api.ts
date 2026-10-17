import type { components } from "./api-types";
export type Profile = components["schemas"]["Profile"];
export type GuardianEvent = components["schemas"]["EventOut"];
export type PoseFrame = components["schemas"]["PoseFrame"];
export type Landmark = components["schemas"]["Landmark"];
export type IngestResult = components["schemas"]["IngestResult"];
export type AppConfig = {
  demo_enabled: boolean;
  notification_mode: string;
  gemini_configured: boolean;
  gemini_billed_project_confirmed: boolean;
  push_public_key: string;
  calls_configured: boolean;
  database: string;
  tiger_budget_limit: number;
};

export async function api<T>(
  path: string,
  method = "GET",
  body?: unknown,
  token?: string,
): Promise<T> {
  const response = await fetch(`/api${path}`, {
    method,
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    const detail = data.detail;
    const validation = Array.isArray(detail)
      ? detail.map((item: { loc?: (string | number)[]; msg?: string }) =>
          `${item.loc?.slice(1).join(".") || "Profile"}: ${item.msg || "Invalid value"}`).join("; ")
      : null;
    throw new Error(validation || (typeof detail === "string"
      ? detail
      : `Request failed (${response.status})`));
  }
  return response.json();
}

export function connectPose(
  onResult: (r: IngestResult) => void,
  onStatus: (s: string) => void,
) {
  let ws: WebSocket | null = null,
    closed = false,
    retry = 0,
    timer: ReturnType<typeof setTimeout> | undefined;
  const connect = () => {
    ws = new WebSocket(
      `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/pose`,
    );
    ws.onopen = () => {
      retry = 0;
      onStatus("Connected");
    };
    ws.onmessage = (e) => {
      const value = JSON.parse(e.data);
      if (value.error) onStatus(value.error);
      else onResult(value);
    };
    ws.onclose = (e) => {
      if (closed) return;
      if (e.code === 4401) {
        onStatus("Session expired. Sign in again.");
        return;
      }
      onStatus("Reconnecting — monitoring interrupted");
      timer = setTimeout(connect, Math.min(10000, 500 * 2 ** retry++));
    };
    ws.onerror = () => onStatus("Connection interrupted");
  };
  connect();
  return {
    send(frame: PoseFrame) {
      if (ws?.readyState === WebSocket.OPEN && ws.bufferedAmount < 128000) {
        ws.send(JSON.stringify(frame));
        return true;
      }
      return false;
    },
    close() {
      closed = true;
      clearTimeout(timer);
      ws?.close();
    },
  };
}

// crypto.randomUUID only exists in secure contexts (HTTPS or localhost); a LAN or
// plain-HTTP host would otherwise crash rendering. getRandomValues works everywhere.
export function uid(): string {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  const b = crypto.getRandomValues(new Uint8Array(16));
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  const h = Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}
