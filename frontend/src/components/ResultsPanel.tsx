import { useMemo, useState } from "react";
import type { ResultCollection, ResultItem } from "../types";

const TAG_LABEL: Record<string, string> = {
  recommended: "★ Recommended",
  cheapest: "Cheapest",
  fastest: "Fastest",
  top_rated: "Top rated",
};

function numeric(value: string | undefined): number | null {
  if (!value) return null;
  const v = value.toLowerCase();
  const h = v.match(/(\d+)\s*(h|hr|hour)/);
  const m = v.match(/(\d+)\s*(m|min)/);
  if (h || m) return (h ? +h[1] * 60 : 0) + (m ? +m[1] : 0);
  const t = v.match(/^(\d{1,2}):(\d{2})\s*(am|pm)?/);
  if (t) return ((+t[1] % 12) + (t[3] === "pm" ? 12 : 0)) * 60 + +t[2];
  const n = v.replace(/,/g, "").match(/\d+(\.\d+)?/);
  return n ? parseFloat(n[0]) : null;
}

function sortItems(items: ResultItem[], column: string | null, dir: 1 | -1) {
  if (!column) return items;
  return [...items].sort((a, b) => {
    const x = numeric(a.fields[column]);
    const y = numeric(b.fields[column]);
    if (x !== null && y !== null) return (x - y) * dir;
    return (a.fields[column] ?? "").localeCompare(b.fields[column] ?? "") * dir;
  });
}

export type SelectMode = "answer" | "follow" | null;

interface SelectProps {
  selectMode: SelectMode;
  busy: boolean;
  onSelect: (itemId: string) => void;
}

function Collection({ c, selectMode, busy, onSelect }: { c: ResultCollection } & SelectProps) {
  const [column, setColumn] = useState<string | null>(null);
  const [dir, setDir] = useState<1 | -1>(1);
  const [filter, setFilter] = useState("");
  const rows = useMemo(() => {
    const f = filter.trim().toLowerCase();
    const filtered = f ? c.items.filter((i) => Object.values(i.fields).some((v) => v.toLowerCase().includes(f))) : c.items;
    return sortItems(filtered, column, dir);
  }, [c.items, column, dir, filter]);
  const recommended = c.items.find((i) => i.id === c.recommendation_id);

  return (
    <div className="collection">
      <div className="collection-head">
        <div>
          <h3>{c.title}</h3>
          {c.source_url && (
            <a className="muted small" href={c.source_url} target="_blank" rel="noreferrer noopener">
              Source: {c.source_title || new URL(c.source_url).hostname}
            </a>
          )}
        </div>
        <div className="collection-tools">
          <input placeholder="Filter…" value={filter} onChange={(e) => setFilter(e.target.value)} aria-label="Filter results" />
          <select
            value={column ?? ""}
            onChange={(e) => setColumn(e.target.value || null)}
            aria-label="Sort by"
          >
            <option value="">Sort: as found</option>
            {c.columns.map((col) => (
              <option key={col} value={col}>
                Sort by {col}
              </option>
            ))}
          </select>
          <button className="btn ghost small" onClick={() => setDir(dir === 1 ? -1 : 1)} disabled={!column} aria-label="Toggle sort direction">
            {dir === 1 ? "↑" : "↓"}
          </button>
        </div>
      </div>
      {recommended && c.recommendation_reason && (
        <div className="recommendation">
          <strong>Recommendation:</strong> {Object.values(recommended.fields)[0]} — {c.recommendation_reason}
        </div>
      )}
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              {c.columns.map((col) => (
                <th
                  key={col}
                  onClick={() => (column === col ? setDir(dir === 1 ? -1 : 1) : (setColumn(col), setDir(1)))}
                  className={column === col ? "sorted" : ""}
                >
                  {col}
                  {column === col ? (dir === 1 ? " ↑" : " ↓") : ""}
                </th>
              ))}
              <th />
            </tr>
          </thead>
          <tbody>
            {rows.map((item) => (
              <tr key={item.id} className={item.tags.includes("recommended") ? "rec" : ""}>
                {c.columns.map((col, i) => (
                  <td key={col} data-label={col}>
                    {i === 0 && item.tags.length > 0 && (
                      <span className="tags">
                        {item.tags.map((t) => (
                          <span key={t} className={`tag tag-${t}`}>
                            {TAG_LABEL[t] ?? t}
                          </span>
                        ))}
                      </span>
                    )}
                    {item.fields[col] ?? "—"}
                  </td>
                ))}
                <td className="row-actions">
                  {selectMode && (
                    <button
                      className={`btn small ${selectMode === "answer" ? "primary" : "ghost"}`}
                      onClick={() => onSelect(item.id)}
                      disabled={busy}
                      title={selectMode === "answer" ? "Tell NOVA this is your choice" : "NOVA opens this item in the browser"}
                    >
                      {selectMode === "answer" ? "Choose this" : "Select ▸"}
                    </button>
                  )}
                  {item.url && (
                    <a href={item.url} target="_blank" rel="noreferrer noopener" className="small">
                      Open ↗
                    </a>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {c.dropped_unverified > 0 && (
        <p className="muted small">
          {c.dropped_unverified} record(s) were discarded because their values could not be found on the page (NOVA never shows unverified data).
        </p>
      )}
    </div>
  );
}

export function ResultsPanel({ results, ...select }: { results: ResultCollection[] } & SelectProps) {
  if (!results.length) return null;
  return (
    <section className={`panel results ${select.selectMode === "answer" ? "choosing" : ""}`} aria-label="Results">
      <div className="panel-head">
        <h2>Results & comparison</h2>
        <span className="muted small">Extracted from live pages and verified against page text</span>
      </div>
      {select.selectMode === "answer" && (
        <div className="choose-hint">NOVA is waiting for your choice. Click <strong>Choose this</strong> on the option you want.</div>
      )}
      {select.selectMode === "follow" && (
        <p className="muted small choose-note">Select any option and NOVA will open it in the browser for you.</p>
      )}
      {results.map((c) => (
        <Collection key={c.id} c={c} {...select} />
      ))}
    </section>
  );
}
