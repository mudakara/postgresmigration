import { useState } from "react";

import type { Category, Severity, Tier } from "../api";

export const CATEGORY_LABEL: Record<Category, string> = {
  blocker: "Blockers",
  execution_safety: "Execution safety",
  semantic_risk: "Silent behaviour risk",
  conversion: "Conversion effort",
};

export const CATEGORY_HELP: Record<Category, string> = {
  blocker: "Cannot be converted as-is.",
  execution_safety: "Decides whether Scenario 2 may run the object inside a rolled-back test transaction.",
  semantic_risk: "Converts cleanly but can return different results — the focus for validating a migration.",
  conversion: "Needs manual rewriting; drives the effort estimate.",
};

export const CATEGORY_ORDER: Category[] = ["blocker", "execution_safety", "semantic_risk", "conversion"];

export const TIER_LABEL: Record<Tier, string> = {
  simple: "Simple",
  moderate: "Moderate",
  complex: "Complex",
  very_complex: "Very complex",
};

export const TIER_ORDER: Tier[] = ["simple", "moderate", "complex", "very_complex"];

export function SeverityChip({ severity }: { severity: Severity }) {
  const icon = severity === "high" ? "▲" : severity === "medium" ? "◆" : "○";
  return (
    <span className={`sev sev-${severity}`} title={`${severity} severity`}>
      <span aria-hidden>{icon}</span> {severity}
    </span>
  );
}

export function TierChip({ tier }: { tier: Tier }) {
  return <span className={`tier tier-${tier}`}>{TIER_LABEL[tier]}</span>;
}

export function UnsafeBadge({ compact = false }: { compact?: boolean }) {
  return (
    <span className="badge badge-warn" title="Unsafe to auto-execute in a rolled-back test transaction">
      ⚠ {compact ? "Unsafe" : "Unsafe to auto-run"}
    </span>
  );
}

export interface BarDatum {
  key: string;
  label: string;
  value: number;
  hint?: string;
}

/** Single-series horizontal bars: one hue, value labels in text ink, hover tooltip. */
export function BarList({
  data,
  onSelect,
  unit = "objects",
}: {
  data: BarDatum[];
  onSelect?: (key: string) => void;
  unit?: string;
}) {
  const [hover, setHover] = useState<string | null>(null);
  const max = Math.max(1, ...data.map((d) => d.value));
  const total = data.reduce((sum, d) => sum + d.value, 0);

  return (
    <div className="barlist" role="list">
      {data.map((d) => {
        const pct = total ? Math.round((d.value / total) * 100) : 0;
        return (
          <button
            key={d.key}
            type="button"
            role="listitem"
            className={`bar-row ${onSelect ? "clickable" : ""}`}
            onClick={onSelect ? () => onSelect(d.key) : undefined}
            onMouseEnter={() => setHover(d.key)}
            onMouseLeave={() => setHover(null)}
            onFocus={() => setHover(d.key)}
            onBlur={() => setHover(null)}
            aria-label={`${d.label}: ${d.value} ${unit} (${pct}%)`}
          >
            <span className="bar-label">{d.label}</span>
            <span className="bar-track">
              {d.value > 0 && <span className="bar-fill" style={{ width: `${(d.value / max) * 100}%` }} />}
              {hover === d.key && (
                <span className="bar-tip" role="tooltip">
                  <strong>{d.value.toLocaleString()}</strong> {unit} · {pct}%{d.hint ? <><br />{d.hint}</> : null}
                </span>
              )}
            </span>
            <span className="bar-value">{d.value.toLocaleString()}</span>
          </button>
        );
      })}
    </div>
  );
}

export function StatTile({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: "warn" | "bad" }) {
  return (
    <div className={`card stat ${tone ? `stat-${tone}` : ""}`}>
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      {sub && <div className="muted small">{sub}</div>}
    </div>
  );
}
