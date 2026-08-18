import type { AgentConfig, AgentEvent, Confidence, Report, RunResult, Verification } from "./types";

const label = (value: string) => value.replace(/_/g, " ");

function tone(kind: "verdict" | "access" | "mcp", value: string): string {
  if (kind === "verdict") {
    if (value === "buildable_today") return "good";
    if (value === "buildable_with_friction") return "warn";
    if (value === "blocked") return "bad";
  }
  if (kind === "access") {
    if (value.startsWith("self_serve")) return "good";
    if (value === "unknown") return "muted";
    return "warn";
  }
  if (kind === "mcp") {
    if (value === "official") return "good";
    if (value === "community") return "warn";
    if (value === "none") return "muted";
  }
  return "muted";
}

export function Pill({ tone: t, children }: { tone: string; children: React.ReactNode }) {
  return <span className={`pill pill-${t}`}>{children}</span>;
}

export function ConfidenceDot({ level }: { level: Confidence }) {
  return <span className={`dot dot-${level}`} title={`${level} confidence`} />;
}

export function SearchBar({
  onSubmit,
  busy,
}: {
  onSubmit: (app: string, forceRefresh: boolean) => void;
  busy: boolean;
}) {
  return (
    <form
      className="searchbar"
      onSubmit={(e) => {
        e.preventDefault();
        const form = e.currentTarget;
        const app = (form.elements.namedItem("app") as HTMLInputElement).value.trim();
        const force = (form.elements.namedItem("force") as HTMLInputElement).checked;
        if (app) onSubmit(app, force);
      }}
    >
      <input
        name="app"
        type="text"
        placeholder="Enter one app name — Zendesk, Linear, Ramp…"
        autoComplete="off"
        disabled={busy}
        autoFocus
      />
      <label className="checkbox">
        <input name="force" type="checkbox" disabled={busy} />
        <span>Ignore cache</span>
      </label>
      <button type="submit" disabled={busy}>
        {busy ? "Researching…" : "Research"}
      </button>
    </form>
  );
}

const NODE_LABELS: Record<string, string> = {
  lookup_cache: "Checking cache",
  resolve: "Resolving product",
  synthesize: "Synthesising report",
  verify: "Verifying claims",
  persist: "Saving",
};

function describe(event: AgentEvent): string | null {
  switch (event.type) {
    case "node_start":
      return NODE_LABELS[String(event.node)] ?? String(event.node);
    case "cache_hit":
      return `Served from cache — ${event.app}`;
    case "resolved":
      return `Resolved to ${event.canonical_name} (${label(String(event.category))})${
        event.ambiguous ? " — name was ambiguous" : ""
      }`;
    case "lane_start":
      return `↳ ${event.label}: searching`;
    case "tool_call":
      return `   ${event.tool}${event.cached ? " (deduped)" : ""} — ${event.urls} URLs`;
    case "lane_done":
      return `↳ ${event.lane}: ${event.findings} findings, ${event.unresolved} unresolved`;
    case "synthesized":
      return `Draft report: verdict ${label(String(event.verdict))}, ${event.evidence} sources`;
    case "verified":
      return event.passed
        ? "Verification passed"
        : `Verification found ${event.issues} issue(s)`;
    case "retry":
      return `Re-researching: ${(event.lanes as string[])?.join(", ")}`;
    case "persisted":
      return "Saved";
    case "error":
      return `Error: ${event.message}`;
    default:
      return null;
  }
}

export function Progress({ events }: { events: AgentEvent[] }) {
  const lines = events.map(describe).filter((line): line is string => Boolean(line));
  if (!lines.length) return <div className="progress"><span className="muted">Starting…</span></div>;
  return (
    <div className="progress">
      {lines.map((line, i) => (
        <div key={i} className={i === lines.length - 1 ? "progress-line active" : "progress-line"}>
          {line}
        </div>
      ))}
    </div>
  );
}

function Field({
  name,
  confidence,
  children,
}: {
  name: string;
  confidence?: Confidence;
  children: React.ReactNode;
}) {
  return (
    <div className="field">
      <div className="field-name">
        {name}
        {confidence && <ConfidenceDot level={confidence} />}
      </div>
      <div className="field-value">{children}</div>
    </div>
  );
}

export function ReportCard({ report }: { report: Report }) {
  return (
    <section className="card">
      <header className="card-head">
        <div>
          <h2>{report.canonical_name}</h2>
          <p className="one-liner">{report.one_liner}</p>
          <p className="meta">
            {label(report.category)}
            {report.vendor && ` · ${report.vendor}`}
            {report.homepage && (
              <>
                {" · "}
                <a href={report.homepage} target="_blank" rel="noreferrer">
                  site
                </a>
              </>
            )}
            {report.docs_url && (
              <>
                {" · "}
                <a href={report.docs_url} target="_blank" rel="noreferrer">
                  docs
                </a>
              </>
            )}
          </p>
        </div>
        <Pill tone={tone("verdict", report.verdict)}>{label(report.verdict)}</Pill>
      </header>

      {report.blocker && (
        <div className="blocker">
          <strong>Blocker</strong> {report.blocker}
        </div>
      )}

      <div className="fields">
        <Field name="Auth" confidence={report.confidence.auth}>
          <div className="pills">
            {report.auth_methods.length ? (
              report.auth_methods.map((m) => (
                <Pill key={m} tone="neutral">
                  {label(m)}
                </Pill>
              ))
            ) : (
              <span className="muted">unknown</span>
            )}
          </div>
          <p>{report.auth_notes}</p>
        </Field>

        <Field name="Access" confidence={report.confidence.access}>
          <Pill tone={tone("access", report.access_tier)}>{label(report.access_tier)}</Pill>
          <p>{report.access_notes}</p>
        </Field>

        <Field name="API surface" confidence={report.confidence.api}>
          <div className="pills">
            {report.api_styles.map((s) => (
              <Pill key={s} tone="neutral">
                {label(s)}
              </Pill>
            ))}
            <Pill tone="muted">{label(report.api_breadth)}</Pill>
          </div>
          <p>{report.api_notes}</p>
        </Field>

        <Field name="MCP" confidence={report.confidence.mcp}>
          <Pill tone={tone("mcp", report.mcp_status)}>{label(report.mcp_status)}</Pill>
          {report.mcp_url && (
            <p>
              <a href={report.mcp_url} target="_blank" rel="noreferrer">
                {report.mcp_url}
              </a>
            </p>
          )}
        </Field>
      </div>

      <Field name="Building a toolkit" confidence={report.confidence.verdict}>
        <p>{report.integration_notes}</p>
      </Field>

      {report.unknowns.length > 0 && (
        <div className="unknowns">
          <div className="field-name">Could not establish</div>
          <ul>
            {report.unknowns.map((u, i) => (
              <li key={i}>{u}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="evidence">
        <div className="field-name">Evidence ({report.evidence.length})</div>
        <ol>
          {report.evidence.map((e, i) => (
            <li key={i}>
              <a href={e.url} target="_blank" rel="noreferrer">
                {e.title || e.url}
              </a>
              <blockquote>{e.quote}</blockquote>
            </li>
          ))}
        </ol>
      </div>
    </section>
  );
}

export function VerificationPanel({
  verification,
  report,
  retries,
}: {
  verification: Verification | null;
  report: Report;
  retries: number;
}) {
  if (!verification) return null;
  return (
    <section className={`card verify ${verification.passed ? "ok" : "flagged"}`}>
      <header className="card-head">
        <h3>Verification</h3>
        <Pill tone={verification.passed ? "good" : "warn"}>
          {verification.passed ? "passed" : "issues found"}
        </Pill>
      </header>
      <p>{verification.summary}</p>

      {retries > 0 && (
        <p className="muted">
          {retries} targeted re-research pass{retries > 1 ? "es" : ""} ran to close gaps.
        </p>
      )}

      {verification.issues.length > 0 && (
        <ul className="issues">
          {verification.issues.map((issue, i) => (
            <li key={i}>
              <ConfidenceDot level={issue.severity} />
              <strong>{issue.field}</strong> — {issue.issue}
            </li>
          ))}
        </ul>
      )}

      {report.human_review_needed && (
        <div className="review">
          <strong>Human review needed.</strong>{" "}
          {report.human_review_reason || "The agent is not confident enough to be trusted here."}
        </div>
      )}
    </section>
  );
}

export function UsageStrip({ result, config }: { result: RunResult; config: AgentConfig | null }) {
  const { usage, tool_stats: tools } = result;
  const cached = usage.cache_read_tokens;
  return (
    <div className="usage">
      <span>
        <strong>{usage.calls}</strong> model calls
      </span>
      <span>
        <strong>{usage.input_tokens.toLocaleString()}</strong> in
        {cached > 0 && <em> +{cached.toLocaleString()} cached</em>}
      </span>
      <span>
        <strong>{usage.output_tokens.toLocaleString()}</strong> out
      </span>
      <span>
        <strong>${usage.cost_usd.toFixed(4)}</strong>
      </span>
      <span>
        <strong>{tools.tool_calls}</strong> tool calls
        {tools.cache_hits > 0 && <em> ({tools.cache_hits} deduped)</em>}
      </span>
      {result.served_from_cache && <span className="pill pill-good">cache hit</span>}
      {config && (
        <span className="muted models">
          {config.orchestrator_model} + {config.worker_model}
        </span>
      )}
    </div>
  );
}
