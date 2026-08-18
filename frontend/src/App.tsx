import { useEffect, useState } from "react";
import { fetchConfig } from "./api";
import {
  Progress,
  ReportCard,
  SearchBar,
  UsageStrip,
  VerificationPanel,
} from "./components";
import type { AgentConfig } from "./types";
import { useResearch } from "./useResearch";

export default function App() {
  const { state, run } = useResearch();
  const [config, setConfig] = useState<AgentConfig | null>(null);

  useEffect(() => {
    fetchConfig().then(setConfig).catch(() => setConfig(null));
  }, []);

  const busy = state.phase === "running";

  return (
    <div className="page">
      <header className="masthead">
        <h1>App Integration Research</h1>
        <p>
          Give it one app name. It researches what auth the API uses, whether a developer can
          get credentials without a sales call, how broad the API surface is, whether an MCP
          server exists — and whether an agent toolkit could be built for it today.
        </p>
      </header>

      <SearchBar onSubmit={run} busy={busy} />

      {config && (
        <p className="pipeline muted">
          {config.lanes.map((l) => l.label).join(" · ")} — run in parallel, then synthesised and
          verified. Budget: {config.max_tool_calls_per_lane} searches per lane,{" "}
          {config.max_verify_retries} repair pass.
        </p>
      )}

      {busy && <Progress events={state.events} />}

      {state.phase === "error" && (
        <div className="card error">
          <strong>Run failed.</strong>
          <p>{state.error}</p>
        </div>
      )}

      {state.result?.report && (
        <>
          <UsageStrip result={state.result} config={config} />
          <ReportCard report={state.result.report} />
          <VerificationPanel
            verification={state.result.verification}
            report={state.result.report}
            retries={state.result.retries}
          />
          <details className="card raw">
            <summary>Run trace &amp; raw JSON</summary>
            <pre>{JSON.stringify(state.result, null, 2)}</pre>
          </details>
        </>
      )}

      <footer className="muted">
        Evidence-first: every claim links to the page it came from, and cited URLs are checked
        against what the agent actually fetched. Unknowns are reported as unknown.
      </footer>
    </div>
  );
}
