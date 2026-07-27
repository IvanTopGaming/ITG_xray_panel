import React, { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import api from '@ui/lib/api';
import { formatBytes, cn } from '@ui/lib/utils';
import {
  formatWith,
  formatDateTimeForLocalInput,
  epochSecFromLocalDateTimeInput,
} from '@ui/lib/datetime';
import {
  TrendingUp,
  TrendingDown,
  Users,
  Activity,
  Globe,
  ArrowUp,
  ArrowDown,
  BarChart3,
  Calendar,
  Layers,
  Search,
  RefreshCw,
  ChevronUp,
  ChevronDown,
} from 'lucide-react';
import { motion, AnimatePresence } from 'framer-motion';

type Period = '1h' | '6h' | '24h' | '7d' | '30d' | '90d' | '365d' | 'all' | 'custom';
type CustomRange = { from: number; to: number };

function periodQuery(period: Period, custom: CustomRange | null): string {
  if (period === 'custom' && custom) return `from=${custom.from}&to=${custom.to}`;
  return `period=${period}`;
}

function fmtRange(c: CustomRange): string {
  const part = (epochSec: number) =>
    formatWith(epochSec * 1000, {
      day: '2-digit',
      month: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    });
  return `${part(c.from)} → ${part(c.to)}`;
}

function toLocalDateTimeInput(ts: number): string {
  return formatDateTimeForLocalInput(ts * 1000);
}

function fromLocalDateTimeInput(s: string): number {
  return epochSecFromLocalDateTimeInput(s);
}
type StatsTab = 'overview' | 'users' | 'inbounds' | 'sites';

interface OverviewData {
  total_up_alltime: number;
  total_down_alltime: number;
  period_up: number;
  period_down: number;
  active_users: number;
  total_users: number;
  active_inbounds: number;
  top_users: Array<{ email: string; inbound_tag: string; up: number; down: number; total: number }>;
  top_inbounds: Array<{ tag: string; protocol: string; up: number; down: number; total: number }>;
  top_domains: Array<{ domain: string; hit_count: number }>;
}

interface TrafficPoint {
  ts: number;
  up: number;
  down: number;
}

interface TrafficData {
  granularity: number;
  points: TrafficPoint[];
}

interface DomainEntry {
  domain: string;
  hit_count: number;
  percent: number;
}

interface UserRankEntry {
  email: string;
  inbound_tag: string;
  up: number;
  down: number;
  total: number;
  enable: boolean;
  last_seen: number;
  limit_bytes: number;
  source_ips?: string[];
}

const PROTOCOL_COLORS: Record<string, string> = {
  vless: 'text-violet-400',
  vmess: 'text-blue-400',
  trojan: 'text-orange-400',
  shadowsocks: 'text-emerald-400',
  wireguard: 'text-cyan-400',
  socks: 'text-yellow-400',
  http: 'text-gray-400',
};

const PROTOCOL_BG: Record<string, string> = {
  vless: 'bg-violet-500/15 border-violet-500/25',
  vmess: 'bg-blue-500/15 border-blue-500/25',
  trojan: 'bg-orange-500/15 border-orange-500/25',
  shadowsocks: 'bg-emerald-500/15 border-emerald-500/25',
  wireguard: 'bg-cyan-500/15 border-cyan-500/25',
  socks: 'bg-yellow-500/15 border-yellow-500/25',
  http: 'bg-gray-500/15 border-gray-500/25',
};

function AreaChart({
  points,
  height = 200,
  showLabels = true,
}: {
  points: TrafficPoint[];
  height?: number;
  showLabels?: boolean;
}) {
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  const W = 800;
  const pad = { t: 12, r: 12, b: showLabels ? 28 : 8, l: showLabels ? 56 : 8 };
  const iW = W - pad.l - pad.r;
  const iH = height - pad.t - pad.b;

  const maxVal = useMemo(() => Math.max(...points.flatMap((p) => [p.up, p.down]), 1), [points]);

  const toX = useCallback(
    (i: number) => pad.l + (i / Math.max(points.length - 1, 1)) * iW,
    [iW, pad.l, points.length]
  );
  const toY = useCallback((v: number) => pad.t + iH * (1 - v / maxVal), [iH, pad.t, maxVal]);

  const linePath = (key: 'up' | 'down') =>
    points
      .map((p, i) => `${i === 0 ? 'M' : 'L'}${toX(i).toFixed(1)},${toY(p[key]).toFixed(1)}`)
      .join(' ');

  const areaPath = (key: 'up' | 'down') => {
    const base = (pad.t + iH).toFixed(1);
    return (
      linePath(key) +
      ` L${toX(points.length - 1).toFixed(1)},${base} L${pad.l.toFixed(1)},${base} Z`
    );
  };

  const yFracs = [0, 0.25, 0.5, 0.75, 1.0];

  const xStep = Math.max(1, Math.floor(points.length / 5));
  const xTicks = points.reduce<Array<{ i: number; ts: number }>>((acc, p, i) => {
    if (i % xStep === 0 || i === points.length - 1) acc.push({ i, ts: p.ts });
    return acc;
  }, []);

  const range = points.length > 1 ? points[points.length - 1].ts - points[0].ts : 0;
  const fmtTime = (ts: number) => {
    if (range < 86400 * 2)
      return formatWith(ts * 1000, { hour: '2-digit', minute: '2-digit', hour12: false });
    return formatWith(ts * 1000, { month: 'short', day: 'numeric' });
  };

  const handleMouseMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const rect = svgRef.current?.getBoundingClientRect();
    if (!rect) return;
    const svgX = ((e.clientX - rect.left) / rect.width) * W;
    const ratio = (svgX - pad.l) / iW;
    const idx = Math.min(points.length - 1, Math.max(0, Math.round(ratio * (points.length - 1))));
    setHoverIdx(idx);
  };

  if (!points.length) {
    return (
      <div
        className="flex flex-col items-center justify-center gap-2 text-gray-600"
        style={{ height }}
      >
        <BarChart3 size={28} />
        <span className="text-sm">No data for this period</span>
      </div>
    );
  }

  const hovered = hoverIdx !== null ? points[hoverIdx] : null;

  return (
    <div ref={containerRef} className="relative select-none">
      <svg
        ref={svgRef}
        viewBox={`0 0 ${W} ${height}`}
        className="w-full"
        style={{ height }}
        onMouseMove={handleMouseMove}
        onMouseLeave={() => setHoverIdx(null)}
      >
        <defs>
          <linearGradient id="gradUp" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="rgb(167,139,250)" stopOpacity="0.35" />
            <stop offset="100%" stopColor="rgb(167,139,250)" stopOpacity="0.02" />
          </linearGradient>
          <linearGradient id="gradDown" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="rgb(96,165,250)" stopOpacity="0.35" />
            <stop offset="100%" stopColor="rgb(96,165,250)" stopOpacity="0.02" />
          </linearGradient>
        </defs>

        {showLabels &&
          yFracs.map((frac) => (
            <line
              key={frac}
              x1={pad.l}
              y1={toY(maxVal * frac)}
              x2={pad.l + iW}
              y2={toY(maxVal * frac)}
              stroke="rgba(255,255,255,0.04)"
              strokeWidth="1"
            />
          ))}

        {showLabels &&
          yFracs.map((frac) => (
            <text
              key={frac}
              x={pad.l - 6}
              y={toY(maxVal * frac) + 4}
              textAnchor="end"
              fontSize="9"
              fill="rgba(255,255,255,0.28)"
              fontFamily="monospace"
            >
              {formatBytes(maxVal * frac, 0)}
            </text>
          ))}

        {showLabels &&
          xTicks.map(({ i, ts }) => (
            <text
              key={i}
              x={toX(i)}
              y={height - 6}
              textAnchor="middle"
              fontSize="9"
              fill="rgba(255,255,255,0.28)"
            >
              {fmtTime(ts)}
            </text>
          ))}

        <path d={areaPath('down')} fill="url(#gradDown)" />
        <path
          d={linePath('down')}
          fill="none"
          stroke="rgb(96,165,250)"
          strokeWidth="1.5"
          strokeLinejoin="round"
        />

        <path d={areaPath('up')} fill="url(#gradUp)" />
        <path
          d={linePath('up')}
          fill="none"
          stroke="rgb(167,139,250)"
          strokeWidth="1.5"
          strokeLinejoin="round"
        />

        {hoverIdx !== null && (
          <>
            <line
              x1={toX(hoverIdx)}
              y1={pad.t}
              x2={toX(hoverIdx)}
              y2={pad.t + iH}
              stroke="rgba(255,255,255,0.15)"
              strokeWidth="1"
              strokeDasharray="3 2"
            />
            <circle
              cx={toX(hoverIdx)}
              cy={toY(points[hoverIdx].up)}
              r="3.5"
              fill="rgb(167,139,250)"
            />
            <circle
              cx={toX(hoverIdx)}
              cy={toY(points[hoverIdx].down)}
              r="3.5"
              fill="rgb(96,165,250)"
            />
          </>
        )}
      </svg>

      <AnimatePresence>
        {hovered && (
          <motion.div
            initial={{ opacity: 0, scale: 0.95 }}
            animate={{ opacity: 1, scale: 1 }}
            exit={{ opacity: 0, scale: 0.95 }}
            transition={{ duration: 0.1 }}
            className="absolute top-2 right-2 bg-[#1a1625]/95 border border-white/10 rounded-xl px-3 py-2 text-xs pointer-events-none shadow-xl"
          >
            <div className="text-gray-400 mb-1.5">
              {formatWith(hovered.ts * 1000, {
                month: 'short',
                day: 'numeric',
                hour: '2-digit',
                minute: '2-digit',
                hour12: false,
              })}
            </div>
            <div className="flex gap-3">
              <span className="flex items-center gap-1 text-violet-400">
                <ArrowUp size={10} /> {formatBytes(hovered.up)}
              </span>
              <span className="flex items-center gap-1 text-blue-400">
                <ArrowDown size={10} /> {formatBytes(hovered.down)}
              </span>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

function StatCard({
  icon: Icon,
  label,
  value,
  sub,
  color = 'violet',
}: {
  icon: React.ElementType;
  label: string;
  value: string;
  sub?: string;
  color?: 'violet' | 'blue' | 'emerald' | 'orange';
}) {
  const colors = {
    violet: 'from-violet-500/15 border-violet-500/20 text-violet-400',
    blue: 'from-blue-500/15 border-blue-500/20 text-blue-400',
    emerald: 'from-emerald-500/15 border-emerald-500/20 text-emerald-400',
    orange: 'from-orange-500/15 border-orange-500/20 text-orange-400',
  };
  const iconBg = {
    violet: 'bg-violet-500/20 text-violet-400',
    blue: 'bg-blue-500/20 text-blue-400',
    emerald: 'bg-emerald-500/20 text-emerald-400',
    orange: 'bg-orange-500/20 text-orange-400',
  };

  return (
    <motion.div
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      className={cn(
        'relative overflow-hidden rounded-2xl border bg-gradient-to-br to-transparent p-4',
        colors[color]
      )}
    >
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-xs text-gray-500 mb-1">{label}</p>
          <p className="text-xl font-bold text-white leading-tight">{value}</p>
          {sub && <p className="text-xs text-gray-500 mt-0.5">{sub}</p>}
        </div>
        <div className={cn('p-2.5 rounded-xl shrink-0', iconBg[color])}>
          <Icon size={18} />
        </div>
      </div>
    </motion.div>
  );
}

function TrafficBar({ up, down, max }: { up: number; down: number; max: number }) {
  const total = up + down;
  const pct = max > 0 ? (total / max) * 100 : 0;
  return (
    <div className="flex items-center gap-2 min-w-[80px]">
      <div className="flex-1 h-1.5 rounded-full bg-white/5 overflow-hidden">
        <div
          className="h-full rounded-full bg-gradient-to-r from-violet-500 to-blue-500"
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

const PERIODS: { value: Period; label: string }[] = [
  { value: '1h', label: '1H' },
  { value: '6h', label: '6H' },
  { value: '24h', label: '24H' },
  { value: '7d', label: '7D' },
  { value: '30d', label: '30D' },
  { value: '90d', label: '90D' },
  { value: '365d', label: '1Y' },
  { value: 'all', label: 'All' },
];

function PeriodSelector({ value, onChange }: { value: Period; onChange: (p: Period) => void }) {
  return (
    <div className="flex items-center gap-1 bg-white/[0.04] p-1 rounded-2xl border border-white/[0.05] overflow-x-auto">
      {PERIODS.map((p) => (
        <button
          key={p.value}
          onClick={() => onChange(p.value)}
          className="relative px-3 py-1.5 text-xs font-bold uppercase tracking-wider rounded-xl transition-colors whitespace-nowrap"
          style={{ color: value === p.value ? '#fff' : 'rgba(156,163,175,1)' }}
        >
          {value === p.value && (
            <motion.div
              layoutId="periodPill"
              className="absolute inset-0 bg-gradient-to-br from-primary/25 to-violet-600/20 rounded-xl border border-white/[0.1] shadow-[0_0_12px_rgba(208,188,255,0.12)]"
              transition={{ type: 'spring', stiffness: 500, damping: 35 }}
            />
          )}
          <span className="relative z-10">{p.label}</span>
        </button>
      ))}
    </div>
  );
}

function CalendarRangeButton({
  active,
  range,
  open,
  onOpenChange,
  onApply,
  onClear,
}: {
  active: boolean;
  range: CustomRange | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onApply: (from: number, to: number) => void;
  onClear: () => void;
}) {
  const nowHour = Math.floor(Date.now() / 1000 / 3600) * 3600;
  const [fromStr, setFromStr] = useState(toLocalDateTimeInput(range?.from ?? nowHour - 24 * 3600));
  const [toStr, setToStr] = useState(toLocalDateTimeInput(range?.to ?? nowHour));
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      const now = Math.floor(Date.now() / 1000 / 3600) * 3600;
      setFromStr(toLocalDateTimeInput(range?.from ?? now - 24 * 3600));
      setToStr(toLocalDateTimeInput(range?.to ?? now));
      setErr(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const apply = () => {
    const f = fromLocalDateTimeInput(fromStr);
    const t = fromLocalDateTimeInput(toStr);
    if (!(f < t)) {
      setErr('"To" must be after "From"');
      return;
    }
    onApply(f, t);
    onOpenChange(false);
  };

  return (
    <div className="relative">
      <button
        onClick={() => onOpenChange(!open)}
        className={cn(
          'flex items-center gap-1.5 px-3 py-1.5 text-xs font-bold uppercase tracking-wider rounded-xl whitespace-nowrap border transition-colors',
          active
            ? 'bg-gradient-to-br from-primary/25 to-violet-600/20 border-white/[0.1] text-white shadow-[0_0_12px_rgba(208,188,255,0.12)]'
            : 'bg-white/[0.04] border-white/[0.05] text-gray-400 hover:text-white'
        )}
      >
        <Calendar size={14} />
        <span>{active && range ? fmtRange(range) : 'Custom'}</span>
        {active && (
          <span
            role="button"
            tabIndex={0}
            onClick={(e) => {
              e.stopPropagation();
              onClear();
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.stopPropagation();
                onClear();
              }
            }}
            className="ml-1 hover:text-red-400"
          >
            ✕
          </span>
        )}
      </button>

      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => onOpenChange(false)} />
          <div className="absolute right-0 top-full mt-2 z-50 w-72 p-4 rounded-2xl border border-white/[0.08] bg-zinc-900/95 backdrop-blur shadow-2xl space-y-3">
            <div className="space-y-1">
              <label className="text-[10px] uppercase tracking-wider text-gray-500">From</label>
              <input
                type="datetime-local"
                value={fromStr}
                onChange={(e) => setFromStr(e.target.value)}
                step={3600}
                className="w-full px-3 py-2 text-sm rounded-lg bg-white/[0.04] border border-white/[0.06] text-white"
              />
            </div>
            <div className="space-y-1">
              <label className="text-[10px] uppercase tracking-wider text-gray-500">To</label>
              <input
                type="datetime-local"
                value={toStr}
                onChange={(e) => setToStr(e.target.value)}
                step={3600}
                className="w-full px-3 py-2 text-sm rounded-lg bg-white/[0.04] border border-white/[0.06] text-white"
              />
            </div>
            {err && <p className="text-xs text-red-400">{err}</p>}
            <div className="flex gap-2 pt-1">
              <button
                onClick={() => onOpenChange(false)}
                className="flex-1 px-3 py-1.5 text-xs font-bold uppercase tracking-wider rounded-lg bg-white/[0.04] border border-white/[0.05] text-gray-300 hover:text-white"
              >
                Cancel
              </button>
              <button
                onClick={apply}
                className="flex-1 px-3 py-1.5 text-xs font-bold uppercase tracking-wider rounded-lg bg-gradient-to-br from-primary/30 to-violet-600/25 border border-white/[0.1] text-white"
              >
                Apply
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

const TABS: { id: StatsTab; label: string; icon: React.ElementType }[] = [
  { id: 'overview', label: 'Overview', icon: Activity },
  { id: 'users', label: 'Users', icon: Users },
  { id: 'inbounds', label: 'Inbounds', icon: Layers },
  { id: 'sites', label: 'Sites', icon: Globe },
];

function SortHeader({
  label,
  sortKey,
  current,
  direction,
  onSort,
}: {
  label: string;
  sortKey: string;
  current: string;
  direction: 'asc' | 'desc';
  onSort: (k: string) => void;
}) {
  const active = current === sortKey;
  return (
    <button
      onClick={() => onSort(sortKey)}
      className={cn(
        'flex items-center gap-1 text-xs font-medium transition-colors',
        active ? 'text-primary' : 'text-gray-500 hover:text-gray-300'
      )}
    >
      {label}
      <span className="flex flex-col gap-px opacity-60">
        <ChevronUp
          size={8}
          className={active && direction === 'asc' ? 'opacity-100' : 'opacity-30'}
        />
        <ChevronDown
          size={8}
          className={active && direction === 'desc' ? 'opacity-100' : 'opacity-30'}
        />
      </span>
    </button>
  );
}

function MiniBar({ value, max, color = 'violet' }: { value: number; max: number; color?: string }) {
  const pct = max > 0 ? Math.min((value / max) * 100, 100) : 0;
  const c = color === 'blue' ? 'bg-blue-500/60' : 'bg-violet-500/60';
  return (
    <div className="w-16 h-1 rounded-full bg-white/5 overflow-hidden">
      <div className={cn('h-full rounded-full', c)} style={{ width: `${pct}%` }} />
    </div>
  );
}

export default function Statistics() {
  const [period, setPeriod] = useState<Period>('7d');
  const [customRange, setCustomRange] = useState<CustomRange | null>(null);
  const [calendarOpen, setCalendarOpen] = useState(false);
  const rangeLabel = period === 'custom' && customRange ? fmtRange(customRange) : period;
  const [tab, setTab] = useState<StatsTab>('overview');
  const [search, setSearch] = useState('');
  const [sortKey, setSortKey] = useState('total');
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('desc');
  const [domainTagFilter, setDomainTagFilter] = useState('');
  const [expandedDomain, setExpandedDomain] = useState<string | null>(null);
  const [selectedUserForChart, setSelectedUserForChart] = useState<{
    email: string;
    inbound_tag: string;
  } | null>(null);
  const [selectedInboundForChart, setSelectedInboundForChart] = useState<string | null>(null);

  const {
    data: overview,
    isLoading: overviewLoading,
    refetch: refetchOverview,
  } = useQuery<OverviewData>({
    queryKey: ['stats-overview', period, customRange],
    queryFn: async () =>
      (await api.get(`/stats/overview?${periodQuery(period, customRange)}`)).data,
    refetchInterval: 30_000,
  });

  const { data: trafficAll } = useQuery<TrafficData>({
    queryKey: ['stats-traffic-all', period, customRange],
    queryFn: async () =>
      (await api.get(`/stats/traffic?${periodQuery(period, customRange)}&entity_type=all`)).data,
    enabled: tab === 'overview',
    refetchInterval: 30_000,
  });

  const { data: trafficUser } = useQuery<TrafficData>({
    queryKey: ['stats-traffic-user', period, customRange, selectedUserForChart],
    queryFn: async () =>
      (
        await api.get(
          `/stats/traffic?${periodQuery(period, customRange)}&entity_type=user` +
            `&entity_id=${encodeURIComponent(selectedUserForChart!.email)}` +
            `&inbound_tag=${encodeURIComponent(selectedUserForChart!.inbound_tag)}`
        )
      ).data,
    enabled: !!selectedUserForChart && tab === 'users',
    refetchInterval: 30_000,
  });

  const { data: trafficInbound } = useQuery<TrafficData>({
    queryKey: ['stats-traffic-inbound', period, customRange, selectedInboundForChart],
    queryFn: async () =>
      (
        await api.get(
          `/stats/traffic?${periodQuery(period, customRange)}&entity_type=inbound&entity_id=${encodeURIComponent(selectedInboundForChart!)}`
        )
      ).data,
    enabled: !!selectedInboundForChart && tab === 'inbounds',
    refetchInterval: 30_000,
  });

  const { data: domainsData, isLoading: domainsLoading } = useQuery({
    queryKey: ['stats-domains', period, customRange, domainTagFilter],
    queryFn: async () =>
      (
        await api.get(
          `/stats/domains?${periodQuery(period, customRange)}&limit=100` +
            (domainTagFilter ? `&inbound_tag=${encodeURIComponent(domainTagFilter)}` : '')
        )
      ).data as { domains: DomainEntry[] },
    enabled: tab === 'sites',
    refetchInterval: 60_000,
  });

  const { data: domainUsersData, isLoading: domainUsersLoading } = useQuery({
    queryKey: ['stats-domain-users', expandedDomain, period, customRange],
    queryFn: async () =>
      (
        await api.get(
          `/stats/domain-users?domain=${encodeURIComponent(expandedDomain!)}&${periodQuery(period, customRange)}`
        )
      ).data as {
        domain: string;
        users: Array<{ email: string; inbound_tag: string; hit_count: number; percent: number }>;
      },
    enabled: !!expandedDomain && tab === 'sites',
  });

  const { data: usersData, isLoading: usersLoading } = useQuery({
    queryKey: ['stats-users', period, customRange],
    queryFn: async () =>
      (await api.get(`/stats/users-ranking?${periodQuery(period, customRange)}`)).data as {
        users: UserRankEntry[];
      },
    enabled: tab === 'users',
    refetchInterval: 30_000,
  });

  const handleSort = (key: string) => {
    if (sortKey === key) setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'));
    else {
      setSortKey(key);
      setSortDir('desc');
    }
  };

  const sortedUsers = useMemo(() => {
    if (!usersData?.users) return [];
    const filtered = search
      ? usersData.users.filter(
          (u) =>
            u.email.toLowerCase().includes(search.toLowerCase()) ||
            u.inbound_tag.toLowerCase().includes(search.toLowerCase())
        )
      : usersData.users;
    return [...filtered].sort((a, b) => {
      const av = (a as any)[sortKey] ?? 0;
      const bv = (b as any)[sortKey] ?? 0;
      return sortDir === 'desc' ? bv - av : av - bv;
    });
  }, [usersData, search, sortKey, sortDir]);

  const sortedDomains = useMemo(() => {
    if (!domainsData?.domains) return [];
    const filtered = search
      ? domainsData.domains.filter((d) => d.domain.toLowerCase().includes(search.toLowerCase()))
      : domainsData.domains;
    return filtered;
  }, [domainsData, search]);

  const maxUserTraffic = useMemo(
    () => Math.max(...(usersData?.users ?? []).map((u) => u.total), 1),
    [usersData]
  );

  const maxIbTraffic = useMemo(
    () => Math.max(...(overview?.top_inbounds ?? []).map((ib) => ib.total), 1),
    [overview]
  );

  const maxDomainHits = useMemo(
    () => Math.max(...sortedDomains.map((d) => d.hit_count), 1),
    [sortedDomains]
  );

  const fmtDate = (ts: number) => {
    if (!ts) return '—';
    return formatWith(ts, {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    });
  };

  return (
    <div className="flex flex-col gap-6 px-4 md:px-6 py-6 max-w-7xl mx-auto">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-white">Statistics</h1>
          <p className="text-sm text-gray-500 mt-0.5">Traffic analytics and usage insights</p>
        </div>
        <div className="flex items-center gap-3">
          <div className="flex items-center gap-2">
            <PeriodSelector
              value={period}
              onChange={(p) => {
                setPeriod(p);
                setCustomRange(null);
              }}
            />
            <CalendarRangeButton
              active={period === 'custom'}
              range={customRange}
              open={calendarOpen}
              onOpenChange={setCalendarOpen}
              onApply={(from, to) => {
                setCustomRange({ from, to });
                setPeriod('custom');
              }}
              onClear={() => {
                setCustomRange(null);
                setPeriod('7d');
              }}
            />
          </div>
          <button
            onClick={() => refetchOverview()}
            className="p-2 rounded-xl bg-white/5 hover:bg-white/10 text-gray-400 hover:text-white transition-colors"
          >
            <RefreshCw size={15} />
          </button>
        </div>
      </div>

      <div className="flex gap-1 bg-white/[0.04] p-1 rounded-2xl border border-white/[0.05] w-fit overflow-x-auto">
        {TABS.map((t) => (
          <button
            key={t.id}
            onClick={() => {
              setTab(t.id);
              setSearch('');
            }}
            className="relative flex items-center gap-2 px-5 py-2 text-xs font-bold uppercase tracking-wider rounded-xl transition-colors whitespace-nowrap"
            style={{ color: tab === t.id ? '#fff' : 'rgba(156,163,175,1)' }}
          >
            {tab === t.id && (
              <motion.div
                layoutId="statsTabPill"
                className="absolute inset-0 bg-gradient-to-br from-primary/25 to-violet-600/20 rounded-xl border border-white/[0.1] shadow-[0_0_12px_rgba(208,188,255,0.12)]"
                transition={{ type: 'spring', stiffness: 500, damping: 35 }}
              />
            )}
            <t.icon size={14} className="relative z-10" />
            <span className="relative z-10">{t.label}</span>
          </button>
        ))}
      </div>

      <AnimatePresence mode="wait">
        {tab === 'overview' && (
          <motion.div
            key="overview"
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -8 }}
            className="flex flex-col gap-6"
          >
            <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
              <StatCard
                icon={TrendingUp}
                label={`Upload (${rangeLabel})`}
                value={formatBytes(overview?.period_up ?? 0)}
                sub={`All time: ${formatBytes(overview?.total_up_alltime ?? 0)}`}
                color="violet"
              />
              <StatCard
                icon={TrendingDown}
                label={`Download (${rangeLabel})`}
                value={formatBytes(overview?.period_down ?? 0)}
                sub={`All time: ${formatBytes(overview?.total_down_alltime ?? 0)}`}
                color="blue"
              />
              <StatCard
                icon={Users}
                label="Active Users"
                value={String(overview?.active_users ?? 0)}
                sub={`${overview?.total_users ?? 0} total`}
                color="emerald"
              />
              <StatCard
                icon={Layers}
                label="Inbounds"
                value={String(overview?.active_inbounds ?? 0)}
                color="orange"
              />
            </div>

            <div className="rounded-2xl border border-white/5 bg-[#1e1b24]/60 p-5">
              <div className="flex items-center justify-between mb-4">
                <h3 className="text-sm font-medium text-gray-300">Traffic Over Time</h3>
                <div className="flex items-center gap-4 text-xs text-gray-500">
                  <span className="flex items-center gap-1.5">
                    <span className="w-3 h-0.5 rounded-full bg-violet-400 inline-block" />
                    Upload
                  </span>
                  <span className="flex items-center gap-1.5">
                    <span className="w-3 h-0.5 rounded-full bg-blue-400 inline-block" />
                    Download
                  </span>
                </div>
              </div>
              <AreaChart points={trafficAll?.points ?? []} height={200} />
            </div>

            <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
              <div className="rounded-2xl border border-white/5 bg-[#1e1b24]/60 p-5">
                <h3 className="text-sm font-medium text-gray-300 mb-4 flex items-center gap-2">
                  <Users size={14} className="text-violet-400" /> Top Users
                </h3>
                {overviewLoading ? (
                  <div className="space-y-2">
                    {[1, 2, 3].map((i) => (
                      <div key={i} className="h-8 rounded-lg skeleton" />
                    ))}
                  </div>
                ) : (
                  <div className="space-y-2">
                    {(overview?.top_users ?? []).slice(0, 5).map((u, i) => (
                      <div key={i} className="flex items-center gap-3">
                        <span className="text-xs text-gray-600 w-4 shrink-0">#{i + 1}</span>
                        <div className="flex-1 min-w-0">
                          <p className="text-xs text-gray-200 truncate">{u.email}</p>
                          <p className="text-[10px] text-gray-600 truncate">{u.inbound_tag}</p>
                        </div>
                        <span className="text-xs text-gray-400 shrink-0">
                          {formatBytes(u.total)}
                        </span>
                      </div>
                    ))}
                    {!overview?.top_users?.length && (
                      <p className="text-xs text-gray-600 text-center py-4">No traffic data yet</p>
                    )}
                  </div>
                )}
              </div>

              <div className="rounded-2xl border border-white/5 bg-[#1e1b24]/60 p-5">
                <h3 className="text-sm font-medium text-gray-300 mb-4 flex items-center gap-2">
                  <Layers size={14} className="text-orange-400" /> Top Inbounds
                </h3>
                {overviewLoading ? (
                  <div className="space-y-2">
                    {[1, 2, 3].map((i) => (
                      <div key={i} className="h-8 rounded-lg skeleton" />
                    ))}
                  </div>
                ) : (
                  <div className="space-y-2">
                    {(overview?.top_inbounds ?? []).slice(0, 5).map((ib, i) => (
                      <div key={i} className="flex items-center gap-3">
                        <span className="text-xs text-gray-600 w-4 shrink-0">#{i + 1}</span>
                        <div className="flex-1 min-w-0">
                          <p className="text-xs text-gray-200 truncate">{ib.tag}</p>
                          <span
                            className={cn(
                              'text-[10px] px-1.5 py-px rounded border',
                              PROTOCOL_BG[ib.protocol] ?? 'bg-gray-500/15 border-gray-500/25',
                              PROTOCOL_COLORS[ib.protocol] ?? 'text-gray-400'
                            )}
                          >
                            {ib.protocol}
                          </span>
                        </div>
                        <span className="text-xs text-gray-400 shrink-0">
                          {formatBytes(ib.total)}
                        </span>
                      </div>
                    ))}
                    {!overview?.top_inbounds?.length && (
                      <p className="text-xs text-gray-600 text-center py-4">No traffic data yet</p>
                    )}
                  </div>
                )}
              </div>

              <div className="rounded-2xl border border-white/5 bg-[#1e1b24]/60 p-5">
                <h3 className="text-sm font-medium text-gray-300 mb-4 flex items-center gap-2">
                  <Globe size={14} className="text-emerald-400" /> Top Sites
                </h3>
                <div className="space-y-2">
                  {(overview?.top_domains ?? []).slice(0, 5).map((d, i) => (
                    <div key={i} className="flex items-center gap-3">
                      <span className="text-xs text-gray-600 w-4 shrink-0">#{i + 1}</span>
                      <p className="flex-1 text-xs text-gray-200 truncate">{d.domain}</p>
                      <span className="text-xs text-gray-500 shrink-0">
                        {d.hit_count.toLocaleString()}
                      </span>
                    </div>
                  ))}
                  {!overview?.top_domains?.length && (
                    <p className="text-xs text-gray-600 text-center py-4">No domain data yet</p>
                  )}
                </div>
              </div>
            </div>
          </motion.div>
        )}

        {tab === 'users' && (
          <motion.div
            key="users"
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -8 }}
            className="flex flex-col gap-4"
          >
            <AnimatePresence>
              {selectedUserForChart && (
                <motion.div
                  initial={{ opacity: 0, height: 0 }}
                  animate={{ opacity: 1, height: 'auto' }}
                  exit={{ opacity: 0, height: 0 }}
                  className="rounded-2xl border border-white/5 bg-[#1e1b24]/60 p-5 overflow-hidden"
                >
                  <div className="flex items-center justify-between mb-3">
                    <div>
                      <p className="text-sm font-medium text-gray-200">
                        {selectedUserForChart.email}
                      </p>
                      <p className="text-xs text-gray-500">{selectedUserForChart.inbound_tag}</p>
                    </div>
                    <button
                      onClick={() => setSelectedUserForChart(null)}
                      className="text-xs text-gray-500 hover:text-gray-300 px-2 py-1 rounded-lg hover:bg-white/5 transition-colors"
                    >
                      Close
                    </button>
                  </div>
                  <AreaChart points={trafficUser?.points ?? []} height={160} />
                  {(() => {
                    const u = sortedUsers.find(
                      (x) =>
                        x.email === selectedUserForChart.email &&
                        x.inbound_tag === selectedUserForChart.inbound_tag
                    );
                    const ips = u?.source_ips ?? [];
                    if (!ips.length) return null;
                    return (
                      <div className="mt-4 pt-4 border-t border-white/5">
                        <div className="flex items-baseline gap-2 mb-2">
                          <span className="text-[10px] uppercase tracking-wider text-gray-500">
                            Recent IPs ({ips.length})
                          </span>
                          {u?.last_seen ? (
                            <span className="text-[10px] text-gray-600">
                              · last seen {fmtDate(u.last_seen)}
                            </span>
                          ) : null}
                        </div>
                        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-1.5">
                          {ips.map((ip, i) => (
                            <span
                              key={`${ip}-${i}`}
                              className={cn(
                                'px-2.5 py-1.5 rounded-lg font-mono text-[11px] truncate border',
                                i === 0
                                  ? 'bg-emerald-500/10 border-emerald-500/25 text-emerald-200'
                                  : 'bg-primary/10 border-primary/20 text-violet-200'
                              )}
                              title={ip}
                            >
                              {ip}
                            </span>
                          ))}
                        </div>
                      </div>
                    );
                  })()}
                </motion.div>
              )}
            </AnimatePresence>

            <div className="relative">
              <Search
                size={14}
                className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-500"
              />
              <input
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Search users..."
                className="w-full bg-white/5 border border-white/5 rounded-xl pl-9 pr-4 py-2.5 text-sm text-gray-200 placeholder-gray-600 focus:outline-none focus:border-white/20 transition-colors"
              />
            </div>

            <div className="rounded-2xl border border-white/5 bg-[#1e1b24]/60 overflow-hidden">
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-white/5">
                      <th className="text-left px-4 py-3 text-xs text-gray-600 w-8">#</th>
                      <th className="text-left px-4 py-3">
                        <SortHeader
                          label="User"
                          sortKey="email"
                          current={sortKey}
                          direction={sortDir}
                          onSort={handleSort}
                        />
                      </th>
                      <th className="text-left px-4 py-3 hidden md:table-cell">
                        <span className="text-xs font-medium text-gray-500">Inbound</span>
                      </th>
                      <th className="text-right px-4 py-3">
                        <SortHeader
                          label="Upload"
                          sortKey="up"
                          current={sortKey}
                          direction={sortDir}
                          onSort={handleSort}
                        />
                      </th>
                      <th className="text-right px-4 py-3">
                        <SortHeader
                          label="Download"
                          sortKey="down"
                          current={sortKey}
                          direction={sortDir}
                          onSort={handleSort}
                        />
                      </th>
                      <th className="text-right px-4 py-3">
                        <SortHeader
                          label="Total"
                          sortKey="total"
                          current={sortKey}
                          direction={sortDir}
                          onSort={handleSort}
                        />
                      </th>
                      <th className="px-4 py-3 hidden lg:table-cell">
                        <span className="text-xs font-medium text-gray-500">Usage</span>
                      </th>
                      <th className="text-right px-4 py-3 hidden lg:table-cell">
                        <SortHeader
                          label="Last Seen"
                          sortKey="last_seen"
                          current={sortKey}
                          direction={sortDir}
                          onSort={handleSort}
                        />
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {usersLoading &&
                      [1, 2, 3, 4, 5].map((i) => (
                        <tr key={i} className="border-b border-white/5">
                          {[1, 2, 3, 4, 5, 6].map((j) => (
                            <td key={j} className="px-4 py-3">
                              <div className="h-4 rounded skeleton" />
                            </td>
                          ))}
                        </tr>
                      ))}
                    {!usersLoading &&
                      sortedUsers.map((u, i) => {
                        const isSelected =
                          selectedUserForChart?.email === u.email &&
                          selectedUserForChart?.inbound_tag === u.inbound_tag;
                        return (
                          <tr
                            key={`${u.email}-${u.inbound_tag}`}
                            onClick={() =>
                              setSelectedUserForChart(
                                isSelected ? null : { email: u.email, inbound_tag: u.inbound_tag }
                              )
                            }
                            className={cn(
                              'border-b border-white/5 cursor-pointer transition-colors',
                              isSelected ? 'bg-primary/5' : 'hover:bg-white/[0.02]'
                            )}
                          >
                            <td className="px-4 py-3 text-xs text-gray-600">{i + 1}</td>
                            <td className="px-4 py-3">
                              <div className="flex items-center gap-2">
                                <div
                                  className={cn(
                                    'w-1.5 h-1.5 rounded-full shrink-0',
                                    u.enable ? 'bg-emerald-500' : 'bg-gray-600'
                                  )}
                                />
                                <span className="text-gray-200 font-medium truncate max-w-[140px]">
                                  {u.email}
                                </span>
                              </div>
                            </td>
                            <td className="px-4 py-3 hidden md:table-cell">
                              <span className="text-xs text-gray-500">{u.inbound_tag}</span>
                            </td>
                            <td className="px-4 py-3 text-right">
                              <span className="text-xs text-violet-400">{formatBytes(u.up)}</span>
                            </td>
                            <td className="px-4 py-3 text-right">
                              <span className="text-xs text-blue-400">{formatBytes(u.down)}</span>
                            </td>
                            <td className="px-4 py-3 text-right">
                              <span className="text-xs text-gray-200 font-medium">
                                {formatBytes(u.total)}
                              </span>
                            </td>
                            <td className="px-4 py-3 hidden lg:table-cell">
                              <MiniBar value={u.total} max={maxUserTraffic} />
                            </td>
                            <td className="px-4 py-3 text-right hidden lg:table-cell">
                              <span className="text-xs text-gray-500">{fmtDate(u.last_seen)}</span>
                            </td>
                          </tr>
                        );
                      })}
                    {!usersLoading && sortedUsers.length === 0 && (
                      <tr>
                        <td colSpan={8} className="px-4 py-10 text-center text-sm text-gray-600">
                          No traffic data for this period
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
              {sortedUsers.length > 0 && (
                <div className="px-4 py-2 border-t border-white/5 text-xs text-gray-600">
                  {sortedUsers.length} users · Click a row to view traffic chart
                </div>
              )}
            </div>
          </motion.div>
        )}

        {tab === 'inbounds' && (
          <motion.div
            key="inbounds"
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -8 }}
            className="flex flex-col gap-4"
          >
            <AnimatePresence>
              {selectedInboundForChart && (
                <motion.div
                  initial={{ opacity: 0, height: 0 }}
                  animate={{ opacity: 1, height: 'auto' }}
                  exit={{ opacity: 0, height: 0 }}
                  className="rounded-2xl border border-white/5 bg-[#1e1b24]/60 p-5 overflow-hidden"
                >
                  <div className="flex items-center justify-between mb-3">
                    <p className="text-sm font-medium text-gray-200">{selectedInboundForChart}</p>
                    <button
                      onClick={() => setSelectedInboundForChart(null)}
                      className="text-xs text-gray-500 hover:text-gray-300 px-2 py-1 rounded-lg hover:bg-white/5 transition-colors"
                    >
                      Close
                    </button>
                  </div>
                  <AreaChart points={trafficInbound?.points ?? []} height={160} />
                </motion.div>
              )}
            </AnimatePresence>

            <div className="rounded-2xl border border-white/5 bg-[#1e1b24]/60 overflow-hidden">
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-white/5">
                      <th className="text-left px-4 py-3 text-xs text-gray-600 w-8">#</th>
                      <th className="text-left px-4 py-3 text-xs text-gray-500">Tag</th>
                      <th className="text-left px-4 py-3 text-xs text-gray-500">Protocol</th>
                      <th className="text-right px-4 py-3 text-xs text-gray-500">Upload</th>
                      <th className="text-right px-4 py-3 text-xs text-gray-500">Download</th>
                      <th className="text-right px-4 py-3 text-xs text-gray-500">Total</th>
                      <th className="px-4 py-3 hidden lg:table-cell text-xs text-gray-500">
                        Share
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {overviewLoading &&
                      [1, 2, 3].map((i) => (
                        <tr key={i} className="border-b border-white/5">
                          {[1, 2, 3, 4, 5, 6].map((j) => (
                            <td key={j} className="px-4 py-3">
                              <div className="h-4 rounded skeleton" />
                            </td>
                          ))}
                        </tr>
                      ))}
                    {!overviewLoading &&
                      (overview?.top_inbounds ?? []).map((ib, i) => {
                        const isSelected = selectedInboundForChart === ib.tag;
                        return (
                          <tr
                            key={ib.tag}
                            onClick={() => setSelectedInboundForChart(isSelected ? null : ib.tag)}
                            className={cn(
                              'border-b border-white/5 cursor-pointer transition-colors',
                              isSelected ? 'bg-primary/5' : 'hover:bg-white/[0.02]'
                            )}
                          >
                            <td className="px-4 py-3 text-xs text-gray-600">{i + 1}</td>
                            <td className="px-4 py-3 font-medium text-gray-200">{ib.tag}</td>
                            <td className="px-4 py-3">
                              <span
                                className={cn(
                                  'text-xs px-2 py-0.5 rounded border',
                                  PROTOCOL_BG[ib.protocol] ?? 'bg-gray-500/15 border-gray-500/25',
                                  PROTOCOL_COLORS[ib.protocol] ?? 'text-gray-400'
                                )}
                              >
                                {ib.protocol}
                              </span>
                            </td>
                            <td className="px-4 py-3 text-right text-xs text-violet-400">
                              {formatBytes(ib.up)}
                            </td>
                            <td className="px-4 py-3 text-right text-xs text-blue-400">
                              {formatBytes(ib.down)}
                            </td>
                            <td className="px-4 py-3 text-right text-xs font-medium text-gray-200">
                              {formatBytes(ib.total)}
                            </td>
                            <td className="px-4 py-3 hidden lg:table-cell">
                              <TrafficBar up={ib.up} down={ib.down} max={maxIbTraffic} />
                            </td>
                          </tr>
                        );
                      })}
                    {!overviewLoading && !overview?.top_inbounds?.length && (
                      <tr>
                        <td colSpan={7} className="px-4 py-10 text-center text-sm text-gray-600">
                          No traffic data for this period
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
              {(overview?.top_inbounds?.length ?? 0) > 0 && (
                <div className="px-4 py-2 border-t border-white/5 text-xs text-gray-600">
                  Click a row to view traffic chart
                </div>
              )}
            </div>
          </motion.div>
        )}

        {tab === 'sites' && (
          <motion.div
            key="sites"
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -8 }}
            className="flex flex-col gap-4"
          >
            <div className="flex flex-col sm:flex-row gap-3">
              <div className="relative flex-1">
                <Search
                  size={14}
                  className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-500"
                />
                <input
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  placeholder="Filter domains..."
                  className="w-full bg-white/5 border border-white/5 rounded-xl pl-9 pr-4 py-2.5 text-sm text-gray-200 placeholder-gray-600 focus:outline-none focus:border-white/20 transition-colors"
                />
              </div>
              <div className="relative">
                <input
                  value={domainTagFilter}
                  onChange={(e) => setDomainTagFilter(e.target.value)}
                  placeholder="Filter by inbound tag..."
                  className="w-full sm:w-52 bg-white/5 border border-white/5 rounded-xl px-4 py-2.5 text-sm text-gray-200 placeholder-gray-600 focus:outline-none focus:border-white/20 transition-colors"
                />
              </div>
            </div>

            <div className="rounded-2xl border border-white/5 bg-[#1e1b24]/60 overflow-hidden">
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-white/5">
                      <th className="text-left px-4 py-3 text-xs text-gray-600 w-8">#</th>
                      <th className="text-left px-4 py-3 text-xs text-gray-500">Domain</th>
                      <th className="text-right px-4 py-3 text-xs text-gray-500">Requests</th>
                      <th className="px-4 py-3 hidden md:table-cell text-xs text-gray-500">
                        Share
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {domainsLoading &&
                      [1, 2, 3, 4, 5].map((i) => (
                        <tr key={i} className="border-b border-white/5">
                          {[1, 2, 3, 4].map((j) => (
                            <td key={j} className="px-4 py-3">
                              <div className="h-4 rounded skeleton" />
                            </td>
                          ))}
                        </tr>
                      ))}
                    {!domainsLoading &&
                      sortedDomains.map((d, i) => {
                        const isExpanded = expandedDomain === d.domain;
                        return (
                          <React.Fragment key={d.domain}>
                            <tr
                              onClick={() => setExpandedDomain(isExpanded ? null : d.domain)}
                              className={cn(
                                'border-b border-white/5 cursor-pointer transition-colors',
                                isExpanded ? 'bg-emerald-500/5' : 'hover:bg-white/[0.02]'
                              )}
                            >
                              <td className="px-4 py-3 text-xs text-gray-600">{i + 1}</td>
                              <td className="px-4 py-3">
                                <div className="flex items-center gap-2">
                                  <motion.div
                                    animate={{ rotate: isExpanded ? 90 : 0 }}
                                    transition={{ duration: 0.2 }}
                                  >
                                    <ChevronDown size={12} className="text-gray-500 shrink-0" />
                                  </motion.div>
                                  <Globe
                                    size={12}
                                    className={cn(
                                      'shrink-0',
                                      isExpanded ? 'text-emerald-400' : 'text-gray-500'
                                    )}
                                  />
                                  <span
                                    className={cn(
                                      'font-medium',
                                      isExpanded ? 'text-emerald-300' : 'text-gray-200'
                                    )}
                                  >
                                    {d.domain}
                                  </span>
                                </div>
                              </td>
                              <td className="px-4 py-3 text-right">
                                <span className="text-gray-300 font-medium">
                                  {d.hit_count.toLocaleString()}
                                </span>
                              </td>
                              <td className="px-4 py-3 hidden md:table-cell">
                                <div className="flex items-center gap-2">
                                  <div className="w-24 h-1.5 rounded-full bg-white/5 overflow-hidden">
                                    <div
                                      className="h-full rounded-full bg-gradient-to-r from-emerald-500/60 to-emerald-400/40"
                                      style={{
                                        width: `${Math.min((d.hit_count / maxDomainHits) * 100, 100)}%`,
                                      }}
                                    />
                                  </div>
                                  <span className="text-xs text-gray-500 w-10 text-right">
                                    {d.percent}%
                                  </span>
                                </div>
                              </td>
                            </tr>

                            <AnimatePresence>
                              {isExpanded && (
                                <tr className="border-b border-white/5">
                                  <td colSpan={4} className="p-0">
                                    <motion.div
                                      initial={{ height: 0, opacity: 0 }}
                                      animate={{ height: 'auto', opacity: 1 }}
                                      exit={{ height: 0, opacity: 0 }}
                                      transition={{ duration: 0.22, ease: 'easeInOut' }}
                                      className="overflow-hidden"
                                    >
                                      <div className="px-6 py-3 bg-emerald-500/[0.03] border-l-2 border-emerald-500/30">
                                        {domainUsersLoading ? (
                                          <div className="flex items-center gap-2 py-2 text-xs text-gray-500">
                                            <RefreshCw size={12} className="animate-spin" />
                                            Loading users...
                                          </div>
                                        ) : !domainUsersData?.users?.length ? (
                                          <p className="text-xs text-gray-600 py-2">
                                            No user data available
                                          </p>
                                        ) : (
                                          <div className="space-y-1.5">
                                            <p className="text-[10px] text-gray-600 uppercase tracking-wider mb-2">
                                              Users accessing {d.domain}
                                            </p>
                                            {domainUsersData.users.map((u, ui) => {
                                              const maxHits =
                                                domainUsersData.users[0]?.hit_count ?? 1;
                                              return (
                                                <div
                                                  key={`${u.email}-${u.inbound_tag}`}
                                                  className="flex items-center gap-3"
                                                >
                                                  <span className="text-[10px] text-gray-600 w-4 shrink-0">
                                                    #{ui + 1}
                                                  </span>
                                                  <div className="flex-1 min-w-0">
                                                    <div className="flex items-center gap-2 mb-0.5">
                                                      <span className="text-xs text-gray-200 truncate font-medium">
                                                        {u.email}
                                                      </span>
                                                      {u.inbound_tag && (
                                                        <span className="text-[10px] text-gray-600 shrink-0">
                                                          {u.inbound_tag}
                                                        </span>
                                                      )}
                                                    </div>
                                                    <div className="flex items-center gap-2">
                                                      <div className="w-32 h-1 rounded-full bg-white/5 overflow-hidden">
                                                        <div
                                                          className="h-full rounded-full bg-emerald-500/50"
                                                          style={{
                                                            width: `${Math.min((u.hit_count / maxHits) * 100, 100)}%`,
                                                          }}
                                                        />
                                                      </div>
                                                      <span className="text-[10px] text-gray-500">
                                                        {u.hit_count.toLocaleString()} req ·{' '}
                                                        {u.percent}%
                                                      </span>
                                                    </div>
                                                  </div>
                                                </div>
                                              );
                                            })}
                                          </div>
                                        )}
                                      </div>
                                    </motion.div>
                                  </td>
                                </tr>
                              )}
                            </AnimatePresence>
                          </React.Fragment>
                        );
                      })}
                    {!domainsLoading && sortedDomains.length === 0 && (
                      <tr>
                        <td colSpan={4} className="px-4 py-10 text-center text-sm text-gray-600">
                          {domainTagFilter || search
                            ? 'No matching domains'
                            : 'No domain data yet — requires Xray access log parsing'}
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
              {sortedDomains.length > 0 && (
                <div className="px-4 py-2 border-t border-white/5 text-xs text-gray-600">
                  {sortedDomains.length} domains · Click a row to see per-user breakdown
                </div>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
