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
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : `Request failed (${response.status})`,
    );
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
