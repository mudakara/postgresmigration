import type { CatalogRelation } from "../api";

interface Node {
  key: string;
  label: string;
  id: string | null;
  title: string;
}

const MAX_PER_SIDE = 12;
const W = 900;
const NODE_W = 250;
const NODE_H = 34;
const GAP = 12;
const X = { left: 8, center: (W - NODE_W) / 2, right: W - NODE_W - 8 };

/** One node per other table; several foreign keys between the same pair share it. */
function nodes(relations: CatalogRelation[], outgoing: boolean): Node[] {
  const byLabel = new Map<string, Node>();
  for (const r of relations) {
    const fk = outgoing
      ? `${r.name ?? "FK"}: (${r.columns.join(", ")}) → ${r.other_label} (${r.other_columns.join(", ")})`
      : `${r.name ?? "FK"}: ${r.other_label} (${r.other_columns.join(", ")}) → (${r.columns.join(", ")})`;
    const node = byLabel.get(r.other_label);
    if (node) node.title += `\n${fk}`;
    else byLabel.set(r.other_label, { key: r.other_label, label: r.other_label, id: r.other_table_id, title: fk });
  }
  return [...byLabel.values()];
}

function fit(text: string, max = 32) {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

interface Props {
  tableId: string;
  label: string;
  references: CatalogRelation[];
  referencedBy: CatalogRelation[];
  onSelect: (id: string) => void;
}

export default function RelationshipDiagram({ tableId, label, references, referencedBy, onSelect }: Props) {
  const self = references.filter((r) => r.other_table_id === tableId);
  const left = nodes(referencedBy.filter((r) => r.other_table_id !== tableId), false);
  const right = nodes(references.filter((r) => r.other_table_id !== tableId), true);
  const shown = { left: left.slice(0, MAX_PER_SIDE), right: right.slice(0, MAX_PER_SIDE) };
  const more = { left: left.length - shown.left.length, right: right.length - shown.right.length };
  const rows = Math.max(shown.left.length + (more.left > 0 ? 1 : 0), shown.right.length + (more.right > 0 ? 1 : 0), 1);
  const height = rows * (NODE_H + GAP) + GAP + 22;
  const top = 22;
  const centerH = Math.max(NODE_H + 18, 52);
  const centerY = top + (rows * (NODE_H + GAP) - GAP) / 2 - centerH / 2;
  const centerMid = centerY + centerH / 2;

  const sideY = (count: number, i: number) => {
    const block = count * (NODE_H + GAP) - GAP;
    return top + (rows * (NODE_H + GAP) - GAP - block) / 2 + i * (NODE_H + GAP);
  };

  const renderSide = (side: "left" | "right") => {
    const list = shown[side];
    const count = list.length + (more[side] > 0 ? 1 : 0);
    const x = X[side];
    return (
      <g>
        {list.map((n, i) => {
          const y = sideY(count, i);
          const mid = y + NODE_H / 2;
          // Arrows point at the referenced (parent) table.
          const path = side === "left"
            ? `M ${x + NODE_W} ${mid} C ${x + NODE_W + 60} ${mid}, ${X.center - 60} ${centerMid}, ${X.center - 2} ${centerMid}`
            : `M ${X.center + NODE_W} ${centerMid} C ${X.center + NODE_W + 60} ${centerMid}, ${x - 60} ${mid}, ${x - 2} ${mid}`;
          return (
            <g key={n.key}>
              <path d={path} className="erd-edge" markerEnd="url(#erd-arrow)" />
              <g
                className={`erd-node ${n.id ? "erd-link" : "erd-outside"}`}
                onClick={n.id ? () => onSelect(n.id!) : undefined}
                role={n.id ? "button" : undefined}
                tabIndex={n.id ? 0 : undefined}
                onKeyDown={n.id ? (e) => { if (e.key === "Enter") onSelect(n.id!); } : undefined}
              >
                <title>{n.title + (n.id ? "" : "\n(not in this snapshot)")}</title>
                <rect x={x} y={y} width={NODE_W} height={NODE_H} rx={7} />
                <text x={x + 12} y={mid + 4}>{fit(n.label)}</text>
              </g>
            </g>
          );
        })}
        {more[side] > 0 && (
          <text x={x + 12} y={sideY(count, list.length) + NODE_H / 2 + 4} className="erd-more">
            + {more[side]} more (see list below)
          </text>
        )}
      </g>
    );
  };

  return (
    <div className="erd">
      <svg viewBox={`0 0 ${W} ${height}`} width="100%" role="img" aria-label={`Foreign keys of ${label}`}>
        <defs>
          <marker id="erd-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M 0 0 L 10 5 L 0 10 z" className="erd-arrowhead" />
          </marker>
        </defs>
        <text x={X.left} y={14} className="erd-heading">{left.length ? `Referenced by (${left.length})` : "Not referenced by any table"}</text>
        <text x={X.right} y={14} className="erd-heading">{right.length ? `References (${right.length})` : "References no table"}</text>
        {renderSide("left")}
        {renderSide("right")}
        <g className="erd-node erd-center">
          <rect x={X.center} y={centerY} width={NODE_W} height={centerH} rx={9} />
          <text x={X.center + NODE_W / 2} y={centerMid + 4} textAnchor="middle">{fit(label, 30)}</text>
        </g>
      </svg>
      {self.length > 0 && (
        <p className="muted small">
          Also references itself: {self.map((r) => `(${r.columns.join(", ")}) → (${r.other_columns.join(", ")})`).join("; ")}
        </p>
      )}
    </div>
  );
}
