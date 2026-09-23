import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { getUserWarnings } from '@/lib/bot';
import { formatDateTime } from '@ui/lib/datetime';
import type { UserWarning } from '@ui/lib/types';

const thresholds: Record<string, string> = {
  traffic_80: '80% used',
  traffic_95: '95% used',
  traffic_exhausted: 'Traffic limit reached',
  expiry_3d: '3 days remaining',
  expiry_1d: '1 day remaining',
  expiry_1h: '1 hour remaining',
  expired: 'Subscription expired',
};

const states: Record<UserWarning['state'], { label: string; tone: string }> = {
  delivered: { label: 'Sent', tone: 'text-emerald-300 border-emerald-500/25 bg-emerald-500/10' },
  pending: { label: 'Queued', tone: 'text-amber-300 border-amber-500/25 bg-amber-500/10' },
  leased: { label: 'Sending', tone: 'text-sky-300 border-sky-500/25 bg-sky-500/10' },
  permanent: { label: 'Failed', tone: 'text-rose-300 border-rose-500/25 bg-rose-500/10' },
  review: { label: 'Needs review', tone: 'text-amber-300 border-amber-500/25 bg-amber-500/10' },
  suppressed: { label: 'Not sent', tone: 'text-white/50 border-white/10 bg-white/5' },
};

const reasons: Record<string, string> = {
  duplicate_warning: 'Duplicate warning skipped',
  stale_generation: 'Subscription or traffic cycle has changed',
  stale_expiry: 'Subscription expiry has changed',
  warning_no_longer_current: 'Warning is no longer relevant',
  user_blocked: 'User is blocked',
  client_missing: 'Access no longer exists',
  client_changed: 'Access ownership has changed',
  legacy_generation_unknown: 'Subscription cycle could not be verified',
  outbox_suppressed: 'Event was already acknowledged',
  live_validation_unavailable: 'Waiting to verify current access',
  telegram_rejected: 'Telegram rejected the message',
};

export function UserWarnings({ telegramId }: { telegramId: number }) {
  const [page, setPage] = useState(0);
  const query = useQuery({
    queryKey: ['bot', 'user-warnings', telegramId, page],
    queryFn: () => getUserWarnings(telegramId, page * 20),
  });
  const history = query.data;
  return (
    <section>
      <h3 className="mb-3 text-base font-semibold text-white">
        Warnings {history && <span className="text-white/40">({history.sent} sent)</span>}
      </h3>
      {query.isLoading && <p className="text-sm text-white/50">Loading warning history…</p>}
      {query.isError && (
        <div role="alert" className="flex items-center justify-between gap-3 text-sm text-rose-300">
          <span>Could not load warning history.</span>
          <button
            type="button"
            onClick={() => query.refetch()}
            className="underline"
            disabled={query.isFetching}
          >
            Retry
          </button>
        </div>
      )}
      {history && (
        <>
          <div className="mb-3 rounded-xl border border-white/[0.05] bg-white/[0.02] px-4 py-3">
            <div className="flex flex-wrap gap-x-4 gap-y-1 text-sm text-white/85">
              <span>Traffic: {history.traffic_sent} sent</span>
              <span>Expiry: {history.expiry_sent} sent</span>
            </div>
            <p className="mt-1 text-xs text-white/50">
              Last {history.days} days · {history.pending} queued / sending · {history.failed}{' '}
              failed / review · {history.suppressed} not sent
            </p>
          </div>
          {history.total === 0 ? (
            <p className="text-sm italic text-white/40">
              No warnings recorded in the last {history.days} days.
            </p>
          ) : (
            <div className="max-h-80 space-y-2 overflow-y-auto pr-1">
              {history.items.map((warning) => {
                const state = states[warning.state];
                return (
                  <div
                    key={warning.id}
                    className="rounded-xl border border-white/[0.05] bg-white/[0.02] px-4 py-3"
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <div className="text-sm font-semibold text-white/90">
                          {warning.type === 'traffic' ? 'Traffic' : 'Expiry'} ·{' '}
                          {thresholds[warning.kind || ''] || warning.kind || 'Unknown threshold'}
                        </div>
                        <div className="mt-1 text-xs text-white/50">
                          {warning.state === 'delivered' ? 'Sent' : 'Updated'}{' '}
                          {formatDateTime(warning.updated_at)}
                        </div>
                        {(warning.node || warning.inbound_tag) && (
                          <div className="mt-1 break-words text-xs text-white/40">
                            {[warning.node, warning.inbound_tag].filter(Boolean).join(' · ')}
                          </div>
                        )}
                      </div>
                      <span
                        className={`shrink-0 rounded-full border px-2 py-0.5 text-xs ${state?.tone || 'text-white/60'}`}
                      >
                        {state?.label || warning.state}
                      </span>
                    </div>
                    {warning.detail && (
                      <p className="mt-2 text-xs text-white/50">
                        {reasons[warning.detail] || 'See delivery details in server logs'}
                      </p>
                    )}
                  </div>
                );
              })}
            </div>
          )}
          {history.total > 20 && (
            <div className="mt-3 flex items-center justify-between text-xs text-white/60">
              <span>
                {history.total} records · page {page + 1} of {Math.ceil(history.total / 20)}
              </span>
              <div className="flex gap-3">
                <button
                  type="button"
                  disabled={page === 0 || query.isFetching}
                  onClick={() => setPage(page - 1)}
                  className="disabled:opacity-40"
                >
                  Previous
                </button>
                <button
                  type="button"
                  disabled={(page + 1) * 20 >= history.total || query.isFetching}
                  onClick={() => setPage(page + 1)}
                  className="disabled:opacity-40"
                >
                  Next
                </button>
              </div>
            </div>
          )}
        </>
      )}
    </section>
  );
}
