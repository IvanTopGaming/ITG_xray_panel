import type { PaymentWorkflow, PaymentStatus } from '@ui/lib/types';
import { PaymentStatusBadge } from './PaymentStatusBadge';

const providerLabels = {
  pending: 'Payment pending',
  waiting_for_capture: 'Awaiting capture',
  succeeded: 'Payment received',
  canceled: 'Payment canceled',
};

const refundLabels = {
  none: 'No refund',
  partial: 'Partial refund; access retained',
  processing: 'Full refund confirmed; access revocation processing',
  pending: 'Full refund confirmed; access revocation pending',
  completed: 'Full refund confirmed; access revoked',
};

export function PaymentState({
  payment,
}: {
  payment: PaymentWorkflow & { status: PaymentStatus };
}) {
  const provider = payment.provider_status;
  const refund = payment.refund_status;
  const delivery = payment.fulfillment_status;
  return (
    <div className="max-w-sm space-y-1 whitespace-normal text-xs">
      {provider ? (
        <span className="inline-flex rounded-full border border-white/15 px-2 py-0.5 text-white/90">
          {providerLabels[provider] || `Payment: ${provider}`}
        </span>
      ) : (
        <div>
          <PaymentStatusBadge status={payment.status} />{' '}
          <span className="text-white/50">Financial state unavailable</span>
        </div>
      )}
      <div className={delivery === 'succeeded' ? 'text-white/60' : 'text-amber-300'}>
        {delivery ? `Delivery: ${delivery}` : 'Delivery state unavailable'}
      </div>
      {payment.checkout_status && payment.checkout_status !== 'ready' && (
        <div className="text-amber-300">{`Checkout: ${payment.checkout_status}`}</div>
      )}
      {refund && refund !== 'none' && (
        <div className={refund === 'completed' ? 'text-white/60' : 'text-amber-300'}>
          {refundLabels[refund] || `Refund: ${refund}`}
          {payment.refunded_amount_kopeks != null &&
            ` · ${(payment.refunded_amount_kopeks / 100).toFixed(2)} ₽`}
        </div>
      )}
      {payment.fulfillment_error && (
        <div className="text-rose-300">{payment.fulfillment_error}</div>
      )}
      {payment.refund_pending_targets?.map((target, index) => (
        <div key={index} className="text-rose-300">
          {`Node ${target.panel_id ?? 'local'}: ${target.error || 'Access revocation pending'}`}
        </div>
      ))}
      {payment.cancel_requested_at && <div className="text-amber-300">Cancellation requested</div>}
    </div>
  );
}
