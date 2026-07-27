import { useEffect, useMemo, useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { motion, AnimatePresence } from 'framer-motion';
import { X, Plus, Trash2, Ban, ShieldCheck, XCircle } from 'lucide-react';
import { toast } from 'react-toastify';
import {
  getBotUser,
  createGrant,
  revokeTariff,
  listTariffs,
  blockBotUser,
  unblockBotUser,
} from '@/lib/bot';
import type { PanelFailure } from '@/lib/bot';
import { ConfirmationModal } from '@ui/components/ui/ConfirmationModal';
import { Select } from '@ui/components/ui/Select';
import { cn } from '@ui/lib/utils';
import type { BotUserDetail, GrantBilling, Tariff, UserTariffGrant, Client } from '@ui/lib/types';

function billingChipClass(billing: GrantBilling): string {
  if (billing === 'paid') return 'border-violet-500/30 bg-violet-500/10 text-violet-300';
  if (billing === 'gift') return 'border-amber-500/30 bg-amber-500/10 text-amber-300';
  return 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300';
}

type ActiveTariffGroup = {
  tariff_id: number;
  tariff: Tariff | undefined;
  clientCount: number;
  earliestExpiry: number | null;
  grant: UserTariffGrant | undefined;
};

interface UserDrawerProps {
  open: boolean;
  telegramId: number | null;
  onClose: () => void;
}

import { formatDate as _formatDay, formatDateTime } from '@ui/lib/datetime';

function formatDate(iso: string | null): string {
  return formatDateTime(iso);
}

function formatDateOnly(input: string | number | null): string {
  return _formatDay(input);
}

function bytes(n: number): string {
  if (n === 0) return '∞';
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MB`;
  return `${(n / 1024 ** 3).toFixed(1)} GB`;
}

export function UserDrawer({ open, telegramId, onClose }: UserDrawerProps) {
  const queryClient = useQueryClient();
  const [showGrantForm, setShowGrantForm] = useState(false);
  const [grantTariffId, setGrantTariffId] = useState<number | null>(null);
  const [grantBilling, setGrantBilling] = useState<GrantBilling>('paid');
  const [grantNote, setGrantNote] = useState('');

  const userQuery = useQuery({
    queryKey: ['bot', 'user', telegramId],
    queryFn: () => getBotUser(telegramId!),
    enabled: open && telegramId !== null,
  });

  const tariffsQuery = useQuery({
    queryKey: ['bot', 'tariffs'],
    queryFn: listTariffs,
    enabled: open,
  });

  useEffect(() => {
    if (!open) {
      setShowGrantForm(false);
      setGrantTariffId(null);
      setGrantBilling('paid');
      setGrantNote('');
      setConfirmRevokeTariffId(null);
    }
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  const grantMutation = useMutation({
    mutationFn: (args: { tariff_id: number; billing: GrantBilling; note?: string }) =>
      createGrant(telegramId!, args),
    onSuccess: () => {
      toast.success('Grant created');
      queryClient.invalidateQueries({ queryKey: ['bot', 'user', telegramId] });
      queryClient.invalidateQueries({ queryKey: ['bot', 'users'] });
      setShowGrantForm(false);
      setGrantNote('');
    },
    onError: (e: unknown) => {
      const msg =
        (e as { response?: { data?: { error?: string } } })?.response?.data?.error ??
        'Grant failed';
      toast.error(msg);
    },
  });

  const warnPanelFailures = (failures?: PanelFailure[]) => {
    if (failures && failures.length) {
      const names = failures.map((f) => f.panel_name || `#${f.panel_id}`).join(', ');
      toast.warning(`Не удалось применить на панелях: ${names}`);
    }
  };

  const blockMutation = useMutation({
    mutationFn: () => blockBotUser(telegramId!),
    onSuccess: (data) => {
      toast.success(
        `Blocked. Cancelled ${data.cancelled_grants} grant(s), disabled ${data.disabled_clients} client(s).`
      );
      warnPanelFailures(data.panel_failures);
      queryClient.invalidateQueries({ queryKey: ['bot', 'user', telegramId] });
      queryClient.invalidateQueries({ queryKey: ['bot', 'users'] });
      queryClient.invalidateQueries({ queryKey: ['bot', 'grants'] });
    },
    onError: () => toast.error('Block failed'),
  });

  const unblockMutation = useMutation({
    mutationFn: () => unblockBotUser(telegramId!),
    onSuccess: (data) => {
      toast.success(
        `Разблокирован. Восстановлено клиентов: ${data.re_enabled + data.remote_re_enabled}.`
      );
      warnPanelFailures(data.panel_failures);
      queryClient.invalidateQueries({ queryKey: ['bot', 'user', telegramId] });
      queryClient.invalidateQueries({ queryKey: ['bot', 'users'] });
    },
    onError: () => toast.error('Unblock failed'),
  });

  const [confirmBlock, setConfirmBlock] = useState(false);
  const [confirmRevokeTariffId, setConfirmRevokeTariffId] = useState<number | null>(null);

  const revokeTariffMutation = useMutation({
    mutationFn: (tariffId: number) => revokeTariff(telegramId!, tariffId),
    onSuccess: (data) => {
      toast.success(
        `Tariff revoked. Disabled ${data.disabled_clients} client(s), removed ${data.revoked_grants} grant(s).`
      );
      queryClient.invalidateQueries({ queryKey: ['bot', 'user', telegramId] });
      queryClient.invalidateQueries({ queryKey: ['bot', 'users'] });
      setConfirmRevokeTariffId(null);
    },
    onError: (e: unknown) => {
      const msg =
        (e as { response?: { data?: { error?: string } } })?.response?.data?.error ??
        'Revoke tariff failed';
      toast.error(msg);
    },
  });

  const submitGrant = () => {
    if (grantTariffId === null) {
      toast.error('Pick a tariff first');
      return;
    }
    grantMutation.mutate({
      tariff_id: grantTariffId,
      billing: grantBilling,
      note: grantNote || undefined,
    });
  };

  const detail: BotUserDetail | undefined = userQuery.data;
  const tariffs: Tariff[] = tariffsQuery.data || [];

  const activeTariffs: ActiveTariffGroup[] = useMemo(() => {
    if (!detail) return [];
    const buckets = new Map<number, Client[]>();
    for (const c of detail.clients) {
      if (!c.enable || c.tariff_id == null) continue;
      const list = buckets.get(c.tariff_id) ?? [];
      list.push(c);
      buckets.set(c.tariff_id, list);
    }
    return Array.from(buckets.entries()).map(([tariff_id, clients]) => {
      const expiries = clients.map((c) => c.expiry_time).filter((e) => e && e > 0) as number[];
      return {
        tariff_id,
        tariff: tariffs.find((t) => t.id === tariff_id),
        clientCount: clients.length,
        earliestExpiry: expiries.length ? Math.min(...expiries) : null,
        grant: detail.grants.find((g) => g.tariff_id === tariff_id),
      };
    });
  }, [detail, tariffs]);

  const revokeTariffDescription = (() => {
    if (!detail || confirmRevokeTariffId === null) return '';
    const group = activeTariffs.find((g) => g.tariff_id === confirmRevokeTariffId);
    const name = group?.tariff?.name ?? `Tariff #${confirmRevokeTariffId}`;
    const who = detail.username ? `@${detail.username}` : `Telegram user ${detail.telegram_id}`;
    const n = group?.clientCount ?? 0;
    return `Revoke "${name}" from ${who}? This disables ${n} active client subscription(s) and removes the access grant. Past traffic statistics are preserved.`;
  })();

  return (
    <AnimatePresence>
      {open && (
        <>
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            onClick={onClose}
            className="fixed inset-0 z-40 bg-black/50"
          />
          <motion.div
            initial={{ x: '100%' }}
            animate={{ x: 0 }}
            exit={{ x: '100%' }}
            transition={{ type: 'spring', stiffness: 300, damping: 30 }}
            className="fixed right-0 top-0 z-50 flex h-full w-full flex-col gap-6 overflow-y-auto border-l border-white/[0.05] bg-zinc-950 px-7 py-6 md:max-w-[640px]"
          >
            <div className="flex items-start justify-between">
              <div className="min-w-0">
                <h2 className="truncate text-xl font-bold text-white">
                  {detail
                    ? detail.username
                      ? `@${detail.username}`
                      : `Telegram user ${detail.telegram_id}`
                    : 'Loading…'}
                </h2>
                {detail && (
                  <p className="mt-1 font-mono text-sm text-white/50">{detail.telegram_id}</p>
                )}
              </div>
              <button
                onClick={onClose}
                className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-white/[0.06] bg-white/[0.04] text-white/60 hover:bg-white/[0.10] hover:text-white"
              >
                <X size={16} />
              </button>
            </div>

            {userQuery.isLoading && <p className="text-base text-white/60">Loading…</p>}
            {userQuery.error && <p className="text-base text-rose-400">Failed to load user.</p>}

            {detail && (
              <>
                {detail.blocked && (
                  <div className="flex items-start gap-3 rounded-xl border border-rose-500/30 bg-rose-500/10 p-4 text-base text-rose-200">
                    <Ban size={20} className="mt-0.5 shrink-0" />
                    <div className="flex-1">
                      <p className="font-semibold">User is blocked</p>
                      <p className="mt-1 text-sm text-rose-300/80">
                        The bot ignores all their messages. Existing subscriptions have been
                        cancelled.
                      </p>
                    </div>
                    <button
                      type="button"
                      onClick={() => unblockMutation.mutate()}
                      disabled={unblockMutation.isPending}
                      className="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-sm font-semibold text-emerald-300 transition-colors hover:border-emerald-500/50 hover:bg-emerald-500/20 hover:text-emerald-200 disabled:opacity-50"
                    >
                      <ShieldCheck size={14} />
                      {unblockMutation.isPending ? 'Unblocking…' : 'Unblock'}
                    </button>
                  </div>
                )}

                <section
                  className={cn(
                    'rounded-xl border bg-white/[0.02] p-5',
                    detail.blocked ? 'border-rose-500/20' : 'border-white/[0.05]'
                  )}
                >
                  <div className="mb-4 flex items-center justify-between">
                    <h3 className="text-base font-semibold text-white">Profile</h3>
                    {!detail.blocked && (
                      <button
                        type="button"
                        onClick={() => setConfirmBlock(true)}
                        className="inline-flex items-center gap-1.5 rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-1.5 text-sm font-semibold text-rose-300 transition-colors hover:border-rose-500/50 hover:bg-rose-500/20 hover:text-rose-200"
                      >
                        <Ban size={14} />
                        Block user
                      </button>
                    )}
                  </div>
                  <dl className="grid grid-cols-[140px_1fr] gap-x-4 gap-y-3 text-base">
                    <dt className="text-white/55">Language</dt>
                    <dd className="font-mono uppercase text-white/90">{detail.language}</dd>

                    <dt className="text-white/55">Trial used</dt>
                    <dd className="text-white/90">
                      {detail.trial_used_at ? formatDate(detail.trial_used_at) : '—'}
                    </dd>

                    <dt className="text-white/55">First seen</dt>
                    <dd className="text-white/90">{formatDate(detail.first_seen_at)}</dd>

                    <dt className="text-white/55">Last seen</dt>
                    <dd className="text-white/90">{formatDate(detail.last_seen_at)}</dd>
                  </dl>
                </section>

                {activeTariffs.length > 0 && (
                  <section>
                    <h3 className="mb-3 text-base font-semibold text-white">
                      Active Tariffs <span className="text-white/40">({activeTariffs.length})</span>
                    </h3>
                    <div className="flex flex-col gap-2">
                      {activeTariffs.map((g) => (
                        <div
                          key={g.tariff_id}
                          className="flex items-center justify-between gap-3 rounded-xl border border-white/[0.05] bg-white/[0.03] px-4 py-3"
                        >
                          <div className="flex min-w-0 flex-col gap-0.5">
                            <span className="flex items-center gap-2 text-sm font-semibold text-white">
                              {g.tariff?.name ?? `Tariff #${g.tariff_id}`}
                              {g.grant && (
                                <span
                                  className={cn(
                                    'rounded-md border px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wider',
                                    billingChipClass(g.grant.billing)
                                  )}
                                >
                                  {g.grant.billing}
                                </span>
                              )}
                            </span>
                            <span className="text-xs text-white/50">
                              {g.clientCount} inbound{g.clientCount === 1 ? '' : 's'}
                              {g.earliestExpiry
                                ? ` · expires ${formatDateOnly(g.earliestExpiry)}`
                                : ' · no expiry'}
                            </span>
                          </div>
                          <button
                            type="button"
                            onClick={() => setConfirmRevokeTariffId(g.tariff_id)}
                            className="inline-flex items-center gap-1.5 rounded-lg border border-rose-500/25 bg-rose-500/10 px-2.5 py-1.5 text-xs font-semibold text-rose-300 transition-colors hover:border-rose-500/40 hover:bg-rose-500/20 hover:text-rose-200"
                          >
                            <XCircle size={13} />
                            Revoke
                          </button>
                        </div>
                      ))}
                    </div>
                  </section>
                )}

                <section>
                  <h3 className="mb-3 text-base font-semibold text-white">
                    Clients <span className="text-white/40">({detail.clients.length})</span>
                  </h3>
                  {detail.clients.length === 0 ? (
                    <p className="text-sm italic text-white/40">No clients</p>
                  ) : (
                    <div className="flex flex-col gap-2">
                      {detail.clients.map((c: Client) => (
                        <div
                          key={c.panel_id != null ? `panel-${c.panel_id}-${c.id}` : `local-${c.id}`}
                          className={cn(
                            'flex items-start justify-between gap-3 rounded-xl border border-white/[0.05] bg-white/[0.03] px-4 py-3',
                            !c.enable && 'opacity-50'
                          )}
                        >
                          <div className="flex min-w-0 flex-1 flex-col gap-1">
                            <span className="flex items-center gap-2">
                              <span
                                className={cn(
                                  'truncate text-sm font-semibold text-white/90',
                                  !c.enable && 'line-through'
                                )}
                              >
                                {c.inbound_tag}
                              </span>
                              {c.panel_name && (
                                <span className="inline-flex shrink-0 items-center rounded-md border border-violet-500/25 bg-violet-500/10 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-violet-300">
                                  {c.panel_name}
                                </span>
                              )}
                            </span>
                            <span
                              className={cn(
                                'truncate font-mono text-xs text-white/45',
                                !c.enable && 'line-through'
                              )}
                            >
                              {c.email}
                            </span>
                            <span className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs text-white/50">
                              <span>Limit {bytes(c.limit_bytes)}</span>
                              <span className="text-white/20">·</span>
                              <span>
                                Expires {c.expiry_time ? formatDateOnly(c.expiry_time) : '—'}
                              </span>
                            </span>
                          </div>
                          <div className="flex shrink-0 flex-col items-end gap-0.5 font-mono text-xs text-white/70">
                            <span className="whitespace-nowrap">↑ {bytes(c.up)}</span>
                            <span className="whitespace-nowrap">↓ {bytes(c.down)}</span>
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </section>

                <section>
                  <div className="mb-3 flex items-center justify-between">
                    <h3 className="text-base font-semibold text-white">
                      Grants <span className="text-white/40">({detail.grants.length})</span>
                    </h3>
                    {!showGrantForm && (
                      <button
                        type="button"
                        onClick={() => setShowGrantForm(true)}
                        className="inline-flex items-center gap-1.5 rounded-lg border border-violet-500/30 bg-gradient-to-br from-violet-500/25 to-violet-600/15 px-3 py-1.5 text-sm font-semibold text-white shadow-[0_0_12px_rgba(208,188,255,0.10)] transition-colors hover:from-violet-500/35"
                      >
                        <Plus size={14} />
                        Grant access
                      </button>
                    )}
                  </div>

                  {showGrantForm && (
                    <div className="mb-3 flex flex-col gap-4 rounded-xl border border-violet-500/20 bg-violet-500/[0.04] p-5">
                      <div className="flex flex-col gap-2">
                        <span className="text-xs font-semibold uppercase tracking-wider text-white/55">
                          Tariff
                        </span>
                        <Select
                          value={grantTariffId === null ? '' : String(grantTariffId)}
                          onChange={(e) =>
                            setGrantTariffId(e.target.value ? parseInt(e.target.value, 10) : null)
                          }
                          options={[
                            { value: '', label: 'Select a tariff…' },
                            ...tariffs.map((t) => ({
                              value: String(t.id),
                              label: `${t.name} · ${t.price_rub}₽ / ${t.period_days}d`,
                            })),
                          ]}
                        />
                      </div>

                      <div className="flex flex-col gap-2">
                        <span className="text-xs font-semibold uppercase tracking-wider text-white/55">
                          Billing
                        </span>
                        <div className="inline-flex rounded-xl border border-white/[0.08] bg-black/30 p-1">
                          <button
                            type="button"
                            onClick={() => setGrantBilling('paid')}
                            className={cn(
                              'flex-1 rounded-lg px-3 py-2 text-sm font-semibold transition-all duration-200',
                              grantBilling === 'paid'
                                ? 'border border-violet-500/40 bg-gradient-to-br from-violet-500/30 to-violet-600/20 text-white shadow-[0_0_10px_rgba(208,188,255,0.15)]'
                                : 'border border-transparent text-white/55 hover:text-white/80'
                            )}
                          >
                            Paid
                          </button>
                          <button
                            type="button"
                            onClick={() => setGrantBilling('gift')}
                            className={cn(
                              'flex-1 rounded-lg px-3 py-2 text-sm font-semibold transition-all duration-200',
                              grantBilling === 'gift'
                                ? 'border border-amber-500/40 bg-gradient-to-br from-amber-500/30 to-amber-600/20 text-white shadow-[0_0_10px_rgba(251,191,36,0.15)]'
                                : 'border border-transparent text-white/55 hover:text-white/80'
                            )}
                          >
                            Gift
                          </button>
                          <button
                            type="button"
                            onClick={() => setGrantBilling('free')}
                            className={cn(
                              'flex-1 rounded-lg px-3 py-2 text-sm font-semibold transition-all duration-200',
                              grantBilling === 'free'
                                ? 'border border-emerald-500/40 bg-gradient-to-br from-emerald-500/30 to-emerald-600/20 text-white shadow-[0_0_10px_rgba(110,231,183,0.15)]'
                                : 'border border-transparent text-white/55 hover:text-white/80'
                            )}
                          >
                            Free
                          </button>
                        </div>
                        <p className="text-xs text-white/45">
                          {grantBilling === 'paid'
                            ? 'Unlocks a private tariff for purchase. User still pays themselves.'
                            : grantBilling === 'gift'
                              ? 'One free period of access as a gift. No auto-renewal — expires naturally.'
                              : 'Lifetime access — auto-renewed by the system without payment.'}
                        </p>
                      </div>

                      <label className="flex flex-col gap-2">
                        <span className="text-xs font-semibold uppercase tracking-wider text-white/55">
                          Note (optional)
                        </span>
                        <input
                          type="text"
                          value={grantNote}
                          onChange={(e) => setGrantNote(e.target.value)}
                          placeholder="e.g. VIP friend, compensation #4214"
                          className="rounded-xl border border-white/[0.08] bg-black/40 px-3.5 py-3 text-base text-white placeholder-white/30 transition-colors focus:border-violet-500/40 focus:outline-none"
                        />
                      </label>

                      <div className="flex justify-end gap-3">
                        <button
                          type="button"
                          onClick={() => setShowGrantForm(false)}
                          className="rounded-xl border border-white/[0.06] bg-white/[0.04] px-5 py-2.5 text-sm font-medium text-white/75 hover:bg-white/[0.08] hover:text-white"
                        >
                          Cancel
                        </button>
                        <button
                          type="button"
                          disabled={grantMutation.isPending}
                          onClick={submitGrant}
                          className="rounded-xl border border-violet-500/40 bg-gradient-to-br from-violet-500/30 to-violet-600/25 px-5 py-2.5 text-sm font-bold text-white shadow-[0_0_12px_rgba(168,85,247,0.20)] transition-colors hover:from-violet-500/40 disabled:opacity-50"
                        >
                          {grantMutation.isPending ? 'Saving…' : 'Save grant'}
                        </button>
                      </div>
                    </div>
                  )}

                  {detail.grants.length === 0 ? (
                    <p className="text-sm italic text-white/40">No grants yet.</p>
                  ) : (
                    <div className="flex flex-col gap-2">
                      {detail.grants.map((g: UserTariffGrant) => {
                        const t = tariffs.find((x) => x.id === g.tariff_id);
                        return (
                          <div
                            key={g.id}
                            className="flex items-center justify-between gap-3 rounded-xl border border-white/[0.05] bg-white/[0.03] px-4 py-3"
                          >
                            <div className="flex min-w-0 flex-col gap-0.5">
                              <span className="flex items-center gap-2 text-sm font-semibold text-white">
                                {t?.name ?? `Tariff #${g.tariff_id}`}
                                <span
                                  className={cn(
                                    'rounded-md border px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wider',
                                    billingChipClass(g.billing)
                                  )}
                                >
                                  {g.billing}
                                </span>
                              </span>
                              <span className="text-xs text-white/50">
                                {g.next_renewal_at
                                  ? `Renews ${formatDate(g.next_renewal_at)}`
                                  : 'No renewal date'}
                                {g.note && <> · {g.note}</>}
                              </span>
                            </div>
                            <button
                              type="button"
                              onClick={() => setConfirmRevokeTariffId(g.tariff_id)}
                              className="inline-flex items-center gap-1.5 rounded-lg border border-rose-500/25 bg-rose-500/10 px-2.5 py-1.5 text-xs font-semibold text-rose-300 transition-colors hover:border-rose-500/40 hover:bg-rose-500/20 hover:text-rose-200"
                            >
                              <Trash2 size={13} />
                              Revoke
                            </button>
                          </div>
                        );
                      })}
                    </div>
                  )}
                </section>

                <section>
                  <h3 className="mb-3 text-base font-semibold text-white">
                    Payments <span className="text-white/40">({detail.payments.length})</span>
                  </h3>
                  {detail.payments.length === 0 ? (
                    <p className="text-sm italic text-white/40">No payments yet.</p>
                  ) : (
                    <div className="flex flex-col gap-1.5">
                      {detail.payments.map((p) => (
                        <div
                          key={p.id}
                          className="flex items-center justify-between gap-3 rounded-xl border border-white/[0.05] bg-white/[0.02] px-4 py-3"
                        >
                          <div className="flex flex-col gap-0.5">
                            <span className="text-sm text-white/85">
                              {formatDate(p.created_at)}
                            </span>
                            <span className="font-mono text-base font-semibold text-white">
                              {p.amount_rub} ₽
                            </span>
                          </div>
                          <span
                            className={cn(
                              'rounded-md border px-2.5 py-1 text-xs font-bold uppercase tracking-wider',
                              p.status === 'succeeded' &&
                                'border-emerald-500/30 bg-emerald-500/10 text-emerald-300',
                              p.status === 'pending' &&
                                'border-amber-500/30 bg-amber-500/10 text-amber-300',
                              p.status === 'cancelled' &&
                                'border-zinc-500/30 bg-zinc-500/10 text-zinc-300',
                              p.status === 'failed' &&
                                'border-rose-500/30 bg-rose-500/10 text-rose-300'
                            )}
                          >
                            {p.status}
                          </span>
                        </div>
                      ))}
                    </div>
                  )}
                </section>
              </>
            )}
          </motion.div>

          <ConfirmationModal
            isOpen={confirmBlock}
            onClose={() => setConfirmBlock(false)}
            onConfirm={() => {
              blockMutation.mutate();
              setConfirmBlock(false);
            }}
            title="Block user"
            description={
              detail
                ? `Block ${detail.username ? `@${detail.username}` : `Telegram user ${detail.telegram_id}`}? The bot will silently ignore all their messages. All ${detail.clients.length} active subscription(s) and any tariff grants will be cancelled. Payment history stays. You can unblock later, but cancelled access has to be re-granted.`
                : ''
            }
            confirmText="Block & cancel access"
            confirmVariant="danger"
            isLoading={blockMutation.isPending}
          />
          <ConfirmationModal
            isOpen={confirmRevokeTariffId !== null}
            onClose={() => setConfirmRevokeTariffId(null)}
            onConfirm={() => {
              if (confirmRevokeTariffId !== null) {
                revokeTariffMutation.mutate(confirmRevokeTariffId);
              }
            }}
            title="Revoke tariff"
            description={revokeTariffDescription}
            confirmText="Revoke tariff"
            confirmVariant="danger"
            isLoading={revokeTariffMutation.isPending}
          />
        </>
      )}
    </AnimatePresence>
  );
}
