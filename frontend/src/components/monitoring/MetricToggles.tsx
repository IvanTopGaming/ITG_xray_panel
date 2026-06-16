export interface MetricDef {
  key: string;
  label: string;
  color: string;
  scope: string;
  metric: string;
  entity: string;
  fmt: (v: number | undefined) => string;
}

export function MetricToggles({
  metrics,
  active,
  onToggle,
}: {
  metrics: MetricDef[];
  active: Set<string>;
  onToggle: (key: string) => void;
}) {
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {metrics.map((m) => {
        const on = active.has(m.key);
        return (
          <button
            key={m.key}
            type="button"
            onClick={() => onToggle(m.key)}
            aria-pressed={on}
            className="flex items-center gap-1.5 px-2.5 py-1 text-xs font-medium rounded-xl border transition-colors"
            style={{
              color: on ? '#fff' : 'rgba(156,163,175,0.7)',
              borderColor: on ? `${m.color}66` : 'rgba(255,255,255,0.06)',
              backgroundColor: on ? `${m.color}1f` : 'rgba(255,255,255,0.02)',
            }}
          >
            <span
              className="inline-block w-2 h-2 rounded-full transition-opacity"
              style={{ backgroundColor: m.color, opacity: on ? 1 : 0.4 }}
            />
            {m.label}
          </button>
        );
      })}
    </div>
  );
}
