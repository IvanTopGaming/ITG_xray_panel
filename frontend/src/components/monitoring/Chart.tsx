import { useState, useRef, useMemo } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { formatWith } from '@/lib/datetime';
import { fmtRate } from './format';

export interface ChartSeries {
  label: string;
  color: string;
  points: { ts: number; value: number }[];
  fmt?: (v: number | undefined) => string;
}

type DrawPoint = { ts: number; raw: number; draw: number };

function hexToRgb(hex: string): string {
  const m = hex.replace('#', '');
  const full =
    m.length === 3
      ? m
          .split('')
          .map((c) => c + c)
          .join('')
      : m;
  const r = parseInt(full.slice(0, 2), 16);
  const g = parseInt(full.slice(2, 4), 16);
  const b = parseInt(full.slice(4, 6), 16);
  return `${r},${g},${b}`;
}

export function Chart({
  series,
  height = 200,
  normalizePerSeries = false,
  onZoom,
}: {
  series: ChartSeries[];
  height?: number;
  normalizePerSeries?: boolean;
  onZoom?: (from: number, to: number) => void;
}) {
  const [hoverTs, setHoverTs] = useState<number | null>(null);
  const [dragStartTs, setDragStartTs] = useState<number | null>(null);
  const [dragCurrentTs, setDragCurrentTs] = useState<number | null>(null);
  const svgRef = useRef<SVGSVGElement>(null);

  const W = 800;
  const pad = { t: 12, r: 12, b: 24, l: 52 };
  const iW = W - pad.l - pad.r;
  const iH = height - pad.t - pad.b;

  const hasData = useMemo(() => series.some((s) => s.points.length > 0), [series]);

  const [tMin, tMax] = useMemo(() => {
    let lo = Infinity;
    let hi = -Infinity;
    for (const s of series)
      for (const p of s.points) {
        if (p.ts < lo) lo = p.ts;
        if (p.ts > hi) hi = p.ts;
      }
    return [lo, hi];
  }, [series]);

  const seriesMax = useMemo(
    () => series.map((s) => Math.max(...s.points.map((p) => p.value), 1)),
    [series]
  );

  const maxVal = useMemo(
    () => Math.max(...series.flatMap((s) => s.points.map((p) => p.value)), 1),
    [series]
  );

  const drawMax = normalizePerSeries ? 1 : maxVal;
  const span = Math.max(tMax - tMin, 1);

  const drawSeries = useMemo<DrawPoint[][]>(
    () =>
      series.map((s, si) =>
        s.points.map((p) => ({
          ts: p.ts,
          raw: p.value,
          draw: normalizePerSeries ? p.value / seriesMax[si] : p.value,
        }))
      ),
    [series, normalizePerSeries, seriesMax]
  );

  const toX = (ts: number) => pad.l + ((ts - tMin) / span) * iW;
  const toY = (v: number) => pad.t + iH * (1 - v / drawMax);

  const linePath = (pts: DrawPoint[]) =>
    pts
      .map((p, i) => `${i === 0 ? 'M' : 'L'}${toX(p.ts).toFixed(1)},${toY(p.draw).toFixed(1)}`)
      .join(' ');

  const areaPath = (pts: DrawPoint[]) => {
    if (!pts.length) return '';
    const base = (pad.t + iH).toFixed(1);
    return (
      linePath(pts) +
      ` L${toX(pts[pts.length - 1].ts).toFixed(1)},${base} L${toX(pts[0].ts).toFixed(1)},${base} Z`
    );
  };

  const yFracs = [0, 0.25, 0.5, 0.75, 1.0];

  const xTicks = useMemo(() => {
    if (!hasData) return [];
    const n = 5;
    return Array.from({ length: n }, (_, k) => tMin + (span * k) / (n - 1));
  }, [tMin, span, hasData]);

  const fmtTime = (ts: number) => {
    if (span < 86400 * 2)
      return formatWith(ts * 1000, { hour: '2-digit', minute: '2-digit', hour12: false });
    return formatWith(ts * 1000, { month: 'short', day: 'numeric' });
  };

  const tsFromEvent = (e: React.MouseEvent<SVGSVGElement>): number | null => {
    const rect = svgRef.current?.getBoundingClientRect();
    if (!rect) return null;
    const svgX = ((e.clientX - rect.left) / rect.width) * W;
    const ratio = Math.min(1, Math.max(0, (svgX - pad.l) / iW));
    return Math.round(tMin + ratio * span);
  };

  const handleMouseMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const ts = tsFromEvent(e);
    if (ts === null) return;
    if (onZoom && dragStartTs !== null) {
      setDragCurrentTs(ts);
      return;
    }
    setHoverTs(ts);
  };

  const handleMouseDown = (e: React.MouseEvent<SVGSVGElement>) => {
    if (!onZoom) return;
    const ts = tsFromEvent(e);
    if (ts === null) return;
    setHoverTs(null);
    setDragStartTs(ts);
    setDragCurrentTs(ts);
  };

  const handleMouseUp = () => {
    if (onZoom && dragStartTs !== null && dragCurrentTs !== null) {
      if (Math.abs(dragCurrentTs - dragStartTs) >= span * 0.02) {
        onZoom(Math.min(dragStartTs, dragCurrentTs), Math.max(dragStartTs, dragCurrentTs));
      }
    }
    setDragStartTs(null);
    setDragCurrentTs(null);
  };

  const handleMouseLeave = () => {
    setHoverTs(null);
    setDragStartTs(null);
    setDragCurrentTs(null);
  };

  if (!hasData) {
    return (
      <div className="flex items-center justify-center text-white/30 text-sm" style={{ height }}>
        No data
      </div>
    );
  }

  const nearest = (pts: DrawPoint[]): DrawPoint | null => {
    if (!pts.length || hoverTs === null) return null;
    let best = pts[0];
    let bd = Math.abs(pts[0].ts - hoverTs);
    for (const p of pts) {
      const d = Math.abs(p.ts - hoverTs);
      if (d < bd) {
        bd = d;
        best = p;
      }
    }
    return best;
  };

  const isDragging = onZoom != null && dragStartTs !== null && dragCurrentTs !== null;
  const hoverX = !isDragging && hoverTs !== null ? toX(hoverTs) : null;

  return (
    <div className="relative select-none">
      <svg
        ref={svgRef}
        viewBox={`0 0 ${W} ${height}`}
        className="w-full"
        style={{ height, cursor: onZoom ? 'crosshair' : undefined }}
        onMouseMove={handleMouseMove}
        onMouseDown={handleMouseDown}
        onMouseUp={handleMouseUp}
        onMouseLeave={handleMouseLeave}
      >
        <defs>
          {series.map((s, si) => {
            const rgb = hexToRgb(s.color);
            return (
              <linearGradient key={si} id={`monGrad${si}`} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={`rgb(${rgb})`} stopOpacity="0.35" />
                <stop offset="100%" stopColor={`rgb(${rgb})`} stopOpacity="0.02" />
              </linearGradient>
            );
          })}
        </defs>

        {yFracs.map((frac) => (
          <line
            key={`g${frac}`}
            x1={pad.l}
            y1={toY(drawMax * frac)}
            x2={pad.l + iW}
            y2={toY(drawMax * frac)}
            stroke="rgba(255,255,255,0.04)"
            strokeWidth="1"
          />
        ))}

        {yFracs.map((frac) => (
          <text
            key={`y${frac}`}
            x={pad.l - 6}
            y={toY(drawMax * frac) + 4}
            textAnchor="end"
            fontSize="9"
            fill="rgba(255,255,255,0.28)"
            fontFamily="monospace"
          >
            {normalizePerSeries ? `${Math.round(frac * 100)}%` : fmtRate(maxVal * frac)}
          </text>
        ))}

        {xTicks.map((ts, i) => (
          <text
            key={`x${i}`}
            x={toX(ts)}
            y={height - 6}
            textAnchor="middle"
            fontSize="9"
            fill="rgba(255,255,255,0.28)"
          >
            {fmtTime(ts)}
          </text>
        ))}

        {series.map((s, si) => (
          <g key={`area${si}`}>
            <path d={areaPath(drawSeries[si])} fill={`url(#monGrad${si})`} />
            <path
              d={linePath(drawSeries[si])}
              fill="none"
              stroke={s.color}
              strokeWidth="1.5"
              strokeLinejoin="round"
            />
          </g>
        ))}

        {isDragging && dragStartTs !== null && dragCurrentTs !== null && (
          <rect
            x={Math.min(toX(dragStartTs), toX(dragCurrentTs))}
            y={pad.t}
            width={Math.abs(toX(dragCurrentTs) - toX(dragStartTs))}
            height={iH}
            fill="rgba(255,255,255,0.08)"
            stroke="rgba(255,255,255,0.25)"
            strokeWidth="1"
          />
        )}

        {hoverX !== null && (
          <>
            <line
              x1={hoverX}
              y1={pad.t}
              x2={hoverX}
              y2={pad.t + iH}
              stroke="rgba(255,255,255,0.15)"
              strokeWidth="1"
              strokeDasharray="3 2"
            />
            {series.map((s, si) => {
              const np = nearest(drawSeries[si]);
              return np ? (
                <circle key={`dot${si}`} cx={toX(np.ts)} cy={toY(np.draw)} r="3.5" fill={s.color} />
              ) : null;
            })}
          </>
        )}
      </svg>

      <AnimatePresence>
        {!isDragging && hoverTs !== null && (
          <motion.div
            initial={{ opacity: 0, scale: 0.95 }}
            animate={{ opacity: 1, scale: 1 }}
            exit={{ opacity: 0, scale: 0.95 }}
            transition={{ duration: 0.1 }}
            className="absolute top-2 right-2 bg-[#1a1625]/95 border border-white/10 rounded-xl px-3 py-2 text-xs pointer-events-none shadow-xl"
          >
            <div className="text-white/40 mb-1.5">
              {formatWith(hoverTs * 1000, {
                month: 'short',
                day: 'numeric',
                hour: '2-digit',
                minute: '2-digit',
                hour12: false,
              })}
            </div>
            <div className="flex flex-col gap-1">
              {series.map((s, si) => {
                const np = nearest(drawSeries[si]);
                return (
                  <span key={si} className="flex items-center gap-1.5" style={{ color: s.color }}>
                    <span
                      className="inline-block w-2 h-2 rounded-full"
                      style={{ backgroundColor: s.color }}
                    />
                    {s.label}: {(s.fmt ?? fmtRate)(np?.raw)}
                  </span>
                );
              })}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
