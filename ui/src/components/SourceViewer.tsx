import { forwardRef, useImperativeHandle, useMemo, useRef } from "react";

import type { Severity } from "../api";

export interface SourceViewerHandle {
  scrollToLine: (line: number) => void;
}

export interface LineMark {
  severity: Severity;
  titles: string[];
}

export function buildMarks(entries: { lines: number[]; severity: Severity; title: string }[]): Map<number, LineMark> {
  const rank = { high: 3, medium: 2, low: 1 } as const;
  const marks = new Map<number, LineMark>();
  for (const e of entries) {
    for (const line of e.lines) {
      const current = marks.get(line);
      if (!current) marks.set(line, { severity: e.severity, titles: [e.title] });
      else {
        current.titles.push(e.title);
        if (rank[e.severity] > rank[current.severity]) current.severity = e.severity;
      }
    }
  }
  return marks;
}

interface Props {
  source: string;
  marks?: Map<number, LineMark>;
  /** Lines outside this range are dimmed (e.g. the rest of a package body). */
  range?: [number, number] | null;
  selected?: number | null;
  onSelect?: (line: number) => void;
  maxHeight?: string;
}

const SourceViewer = forwardRef<SourceViewerHandle, Props>(function SourceViewer(
  { source, marks, range, selected, onSelect, maxHeight },
  ref,
) {
  const container = useRef<HTMLDivElement>(null);
  const lines = useMemo(() => source.replace(/\n$/, "").split("\n"), [source]);

  useImperativeHandle(ref, () => ({
    scrollToLine(line: number) {
      const box = container.current;
      const el = box?.querySelector(`[data-line="${line}"]`) as HTMLElement | null;
      if (box && el) {
        box.scrollIntoView({ behavior: "smooth", block: "nearest" });
        box.scrollTo({ top: el.offsetTop - box.clientHeight / 3, behavior: "smooth" });
      }
    },
  }));

  return (
    <div className="source" ref={container} style={maxHeight ? { maxHeight } : undefined}>
      {lines.map((text, i) => {
        const n = i + 1;
        const mark = marks?.get(n);
        const outside = range ? n < range[0] || n > range[1] : false;
        return (
          <div
            key={n}
            data-line={n}
            className={`src-line ${mark ? `flag-${mark.severity}` : ""} ${selected === n ? "selected" : ""} ${outside ? "dim" : ""}`}
            title={mark?.titles.join(" · ")}
            onClick={onSelect ? () => onSelect(n) : undefined}
          >
            <span className="src-no">{n}</span>
            <span className="src-mark" aria-hidden>{mark ? "●" : ""}</span>
            <span className="src-text">{text || " "}</span>
          </div>
        );
      })}
    </div>
  );
});

export default SourceViewer;
