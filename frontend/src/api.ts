import type { AgentConfig, AgentEvent, RunResult } from "./types";

const BASE = "/api";

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const detail = await res.text().catch(() => res.statusText);
    throw new Error(`${res.status}: ${detail.slice(0, 300)}`);
  }
  return (await res.json()) as T;
}

export async function fetchConfig(): Promise<AgentConfig> {
  return json<AgentConfig>(await fetch(`${BASE}/config`));
}

export async function startRun(
  appName: string,
  forceRefresh: boolean,
): Promise<{ run_id: string }> {
  const res = await fetch(`${BASE}/research`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ app_name: appName, force_refresh: forceRefresh }),
  });
  return json<{ run_id: string }>(res);
}

export async function fetchRun(runId: string): Promise<RunResult> {
  return json<RunResult>(await fetch(`${BASE}/runs/${runId}`));
}

export function subscribeToRun(
  runId: string,
  onEvent: (event: AgentEvent) => void,
  onClose: () => void,
): () => void {
  const source = new EventSource(`${BASE}/research/${runId}/events`);
  let closed = false;

  const finish = () => {
    if (closed) return;
    closed = true;
    source.close();
    onClose();
  };

  source.onmessage = (message) => {
    try {
      onEvent(JSON.parse(message.data) as AgentEvent);
    } catch {

    }
  };

  for (const name of [
    "node_start",
    "resolved",
    "lane_start",
    "tool_call",
    "lane_done",
    "synthesized",
    "verified",
    "retry",
    "persisted",
    "cache_hit",
  ]) {
    source.addEventListener(name, (event) => {
      try {
        onEvent(JSON.parse((event as MessageEvent).data) as AgentEvent);
      } catch {

      }
    });
  }

  source.addEventListener("done", finish);
  source.addEventListener("error", (event) => {

    const data = (event as MessageEvent).data;
    if (data) {
      try {
        onEvent(JSON.parse(data) as AgentEvent);
      } catch {

      }
    }
    finish();
  });

  return finish;
}
