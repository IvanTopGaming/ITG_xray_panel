import { motion } from 'framer-motion';

const RANGES = [
  { value: 'live', label: 'Live' },
  { value: '1h', label: '1h' },
  { value: '24h', label: '24h' },
  { value: '7d', label: '7d' },
  { value: '30d', label: '30d' },
];

export function RangeSelector({
  value,
  onChange,
}: {
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <div className="flex items-center gap-1 bg-white/[0.04] p-1 rounded-2xl border border-white/[0.05] overflow-x-auto">
      {RANGES.map((r) => (
        <button
          key={r.value}
          onClick={() => onChange(r.value)}
          className="relative px-3 py-1.5 text-xs font-bold uppercase tracking-wider rounded-xl transition-colors whitespace-nowrap"
          style={{ color: value === r.value ? '#fff' : 'rgba(156,163,175,1)' }}
        >
          {value === r.value && (
            <motion.div
              layoutId="monRangePill"
              className="absolute inset-0 bg-gradient-to-br from-primary/25 to-violet-600/20 rounded-xl border border-white/[0.1] shadow-[0_0_12px_rgba(208,188,255,0.12)]"
              transition={{ type: 'spring', stiffness: 500, damping: 35 }}
            />
          )}
          <span className="relative z-10">{r.label}</span>
        </button>
      ))}
    </div>
  );
}
