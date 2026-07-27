import { PaymentStatus } from '@ui/lib/types';

const styles: Record<PaymentStatus, string> = {
  succeeded: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/30',
  pending: 'bg-amber-500/15 text-amber-300 border-amber-500/30',
  cancelled: 'bg-white/5 text-white/50 border-white/10',
  failed: 'bg-rose-500/15 text-rose-300 border-rose-500/30',
};

const labels: Record<PaymentStatus, string> = {
  succeeded: 'Paid',
  pending: 'Pending',
  cancelled: 'Cancelled',
  failed: 'Failed',
};

export function PaymentStatusBadge({ status }: { status: PaymentStatus }) {
  return (
    <span
      className={`inline-flex items-center px-2 py-0.5 text-xs font-medium rounded-full border ${styles[status]}`}
    >
      {labels[status]}
    </span>
  );
}
