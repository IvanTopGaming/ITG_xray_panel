import { PaymentStatus } from '@ui/lib/types';

const styles: Record<PaymentStatus, string> = {
  succeeded: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/30',
  pending: 'bg-amber-500/15 text-amber-300 border-amber-500/30',
  processing: 'bg-sky-500/15 text-sky-300 border-sky-500/30',
  refunded: 'bg-violet-500/15 text-violet-300 border-violet-500/30',
  cancelled: 'bg-white/5 text-white/50 border-white/10',
  failed: 'bg-rose-500/15 text-rose-300 border-rose-500/30',
};

const labels: Record<PaymentStatus, string> = {
  succeeded: 'Paid',
  pending: 'Pending',
  processing: 'Processing',
  refunded: 'Refunded',
  cancelled: 'Cancelled',
  failed: 'Failed',
};

export function PaymentStatusBadge({ status }: { status: PaymentStatus }) {
  return (
    <span
      className={`inline-flex items-center px-2 py-0.5 text-xs font-medium rounded-full border ${styles[status] || styles.cancelled}`}
    >
      {labels[status] || status || 'Unknown'}
    </span>
  );
}
