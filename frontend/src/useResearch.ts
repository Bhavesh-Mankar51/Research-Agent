import { useCallback, useRef, useState } from "react";
import { fetchRun, startRun, subscribeToRun } from "./api";
import type { AgentEvent, RunResult } from "./types";

export type Phase = "idle" | "running" | "done" | "error";

export interface ResearchState {
  phase: Phase;
  runId: string | null;
  events: AgentEvent[];
  result: RunResult | null;
  error: string | null;
}

const INITIAL: ResearchState = {
  phase: "idle",
  runId: null,
  events: [],
  result: null,
  error: null,
};

export function useResearch() {
  const [state, setState] = useState<ResearchState>(INITIAL);
  const unsubscribe = useRef<(() => void) | null>(null);

  const run = useCallback(async (appName: string, forceRefresh: boolean) => {
    unsubscribe.current?.();
    setState({ ...INITIAL, phase: "running" });

    let runId: string;
    try {
      ({ run_id: runId } = await startRun(appName, forceRefresh));
    } catch (err) {
      setState({ ...INITIAL, phase: "error", error: (err as Error).message });
      return;
    }

    setState((prev) => ({ ...prev, runId }));

    unsubscribe.current = subscribeToRun(
      runId,
      (event) => setState((prev) => ({ ...prev, events: [...prev.events, event] })),
      async () => {

        try {
          const result = await fetchRun(runId);
          setState((prev) => ({
            ...prev,
            result,
            phase: result.report ? "done" : "error",
            error: result.report ? null : (result.error ?? "Run produced no report."),
          }));
        } catch (err) {
          setState((prev) => ({ ...prev, phase: "error", error: (err as Error).message }));
        }
      },
    );
  }, []);

  const reset = useCallback(() => {
    unsubscribe.current?.();
    setState(INITIAL);
  }, []);

  return { state, run, reset };
}
