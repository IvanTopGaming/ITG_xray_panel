import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { AnimatePresence, motion } from 'framer-motion';
import { ExternalLink } from 'lucide-react';
import { Select } from '@/components/ui/Select';
import { listPayments, PaymentListFilters } from '../../lib/bot';
import { PaymentStatusBadge } from './PaymentStatusBadge';
import { PaymentStatus } from '../../lib/types';
import { formatDateTime as formatDate } from '@/lib/datetime';

const STATUS_SELECT_OPTIONS: { value: string; label: string }[] = [
  { value: 'all', label: 'All statuses' },
  { value: 'succeeded', label: 'Succeeded' },
  { value: 'pending', label: 'Pending' },
  { value: 'cancelled', label: 'Cancelled' },
  { value: 'failed', label: 'Failed' },
];

export function PaymentsTab() {
  const [status, setStatus] = useState<'all' | PaymentStatus>('all');
  const [telegramId, setTelegramId] = useState('');
  const [from, setFrom] = useState('');
  const [to, setTo] = useState('');

  const filters: PaymentListFilters = {};
  if (status !== 'all') filters.status = status;
  if (telegramId) filters.telegram_id = Number(telegramId);
  if (from) filters.from = from;
  if (to) filters.to = to;

  const q = useQuery({
    queryKey: ['payments', status, telegramId, from, to],
    queryFn: () => listPayments(filters),
  });

  const reset = () => {
    setStatus('all');
    setTelegramId('');
    setFrom('');
    setTo('');
  };

  const hasFilters = status !== 'all' || telegramId !== '' || from !== '' || to !== '';

  return (
    <div className="space-y-4">
      <div className="relative overflow-hidden rounded-2xl border border-white/[0.05] bg-gradient-to-br from-white/[0.04] to-white/[0.01] p-5 shadow-sm">
        <div className="absolute -right-12 -top-12 h-32 w-32 rounded-full bg-primary/10 blur-[40px]" />
        {q.data ? (
          <div className="relative z-10 flex items-center gap-6">
            <div>
              <div className="mb-1 text-xs uppercase tracking-wider text-white/50">This month</div>
              <div className="text-2xl font-semibold tracking-tight text-white">
                {q.data.stats.month_count} payments <span className="mx-2 text-white/20">·</span>{' '}
                <span className="text-primary-100">{q.data.stats.month_amount_rub} ₽</span>
              </div>
            </div>
          </div>
        ) : (
          <div className="text-sm text-white/40">Loading…</div>
        )}
      </div>

      <div className="grid grid-cols-1 gap-3 rounded-2xl border border-white/[0.05] bg-white/[0.02] p-4 sm:grid-cols-2 md:grid-cols-5">
        <Select
          value={status}
          onChange={(e) => setStatus(e.target.value as 'all' | PaymentStatus)}
          options={STATUS_SELECT_OPTIONS}
        />
        <input
          placeholder="Telegram ID"
          value={telegramId}
          onChange={(e) => setTelegramId(e.target.value.replace(/\D/g, ''))}
          className="rounded-xl border border-white/[0.08] bg-black/40 px-3 py-2.5 text-sm text-white placeholder-white/30 transition-colors focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary/50"
        />
        <input
          type="date"
          value={from}
          onChange={(e) => setFrom(e.target.value)}
          className="rounded-xl border border-white/[0.08] bg-black/40 px-3 py-2.5 text-sm text-white transition-colors focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary/50 [color-scheme:dark]"
        />
        <input
          type="date"
          value={to}
          onChange={(e) => setTo(e.target.value)}
          className="rounded-xl border border-white/[0.08] bg-black/40 px-3 py-2.5 text-sm text-white transition-colors focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary/50 [color-scheme:dark]"
        />
        <button
          onClick={reset}
          disabled={!hasFilters}
          className="rounded-xl border border-white/[0.05] bg-white/[0.05] px-4 py-2.5 text-sm font-medium text-white/80 transition-colors hover:bg-white/[0.10] hover:text-white disabled:cursor-not-allowed disabled:opacity-40"
        >
          Reset filters
        </button>
      </div>

      <div className="overflow-x-auto rounded-2xl border border-white/[0.05] bg-white/[0.02]">
        <table className="w-full text-sm whitespace-nowrap">
          <thead className="bg-black/40 text-left text-xs uppercase tracking-wider text-white/50">
            <tr>
              <th className="px-4 py-3 font-medium">Date</th>
              <th className="px-4 py-3 font-medium">TG ID</th>
              <th className="px-4 py-3 font-medium">Tariff</th>
              <th className="px-4 py-3 text-right font-medium">Amount</th>
              <th className="px-4 py-3 font-medium">Status</th>
              <th className="px-4 py-3 font-medium">YooKassa</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-white/[0.04]">
            <AnimatePresence>
              {q.data?.items.map((p) => (
                <motion.tr
                  key={p.id}
                  layout="position"
                  variants={{
                    initial: { opacity: 0, y: 6 },
                    animate: {
                      opacity: 1,
                      y: 0,
                      transition: { duration: 0.25, ease: [0.25, 0.46, 0.45, 0.94] },
                    },
                    exit: {
                      opacity: 0,
                      x: -20,
                      transition: { duration: 0.18, ease: [0.4, 0, 1, 1] },
                    },
                  }}
                  transition={{ layout: { type: 'spring', stiffness: 400, damping: 35 } }}
                  initial="initial"
                  animate="animate"
                  exit="exit"
                  className="transition-colors hover:bg-white/[0.04]"
                >
                  <td className="px-4 py-3 text-white/60">{formatDate(p.created_at)}</td>
                  <td className="px-4 py-3 font-mono text-white/90">{p.telegram_id}</td>
                  <td className="px-4 py-3 text-white/90">{p.tariff_name || '—'}</td>
                  <td className="px-4 py-3 text-right font-medium text-white/90">
                    {p.amount_rub} ₽
                  </td>
                  <td className="px-4 py-3">
                    <PaymentStatusBadge status={p.status} />
                  </td>
                  <td className="px-4 py-3 text-right">
                    <a
                      href={`https://yookassa.ru/my/payments/${p.yookassa_id}`}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex items-center gap-1.5 rounded-lg border border-primary/30 bg-primary/10 px-3 py-1.5 text-xs font-semibold text-primary-100 transition-all hover:-translate-y-px hover:border-primary/50 hover:bg-primary/20 hover:text-white hover:shadow-[0_0_12px_rgba(208,188,255,0.18)]"
                    >
                      Open
                      <ExternalLink size={12} />
                    </a>
                  </td>
                </motion.tr>
              ))}
              {q.data && q.data.items.length === 0 && (
                <motion.tr
                  key="empty"
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  exit={{ opacity: 0 }}
                  transition={{ duration: 0.15 }}
                >
                  <td colSpan={6} className="px-4 py-12 text-center text-white/40">
                    No payments match the filters.
                  </td>
                </motion.tr>
              )}
            </AnimatePresence>
          </tbody>
        </table>
      </div>
    </div>
  );
}
