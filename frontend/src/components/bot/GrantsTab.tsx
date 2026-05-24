import { useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { toast } from 'react-toastify';
import { Plus, Trash2 } from 'lucide-react';
import { Modal } from '@/components/ui/Modal';
import { ConfirmationModal } from '@/components/ui/ConfirmationModal';
import { Select } from '@/components/ui/Select';
import { Button } from '@/components/ui/Button';
import { cn } from '@/lib/utils';
import { listGrants, createGrant, revokeTariff, listTariffs, listBotUsers } from '@/lib/bot';
import type { GrantRow, Tariff, BotUser, GrantBilling } from '@/lib/types';

function billingChipClass(billing: GrantBilling): string {
  if (billing === 'paid') return 'border-violet-500/30 bg-violet-500/10 text-violet-300';
  if (billing === 'gift') return 'border-amber-500/30 bg-amber-500/10 text-amber-300';
  return 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300';
}

import { formatDateTime as formatDate } from '@/lib/datetime';

interface AddDialogProps {
  open: boolean;
  tariffs: Tariff[];
  botUsers: BotUser[];
  onClose: () => void;
  onSubmit: (args: {
    tg_id: number;
    tariff_id: number;
    billing: GrantBilling;
    note: string;
  }) => void;
  submitting: boolean;
}

function AddDialog({ open, tariffs, botUsers, onClose, onSubmit, submitting }: AddDialogProps) {
  const [tgIdStr, setTgIdStr] = useState('');
  const [tariffId, setTariffId] = useState<number | null>(null);
  const [billing, setBilling] = useState<GrantBilling>('paid');
  const [note, setNote] = useState('');

  useEffect(() => {
    if (open) {
      setTgIdStr('');
      setTariffId(null);
      setBilling('paid');
      setNote('');
    }
  }, [open]);

  const handleSubmit = () => {
    const tgId = parseInt(tgIdStr, 10);
    if (!tgId || !tariffId) {
      toast.error('Telegram ID and tariff are required');
      return;
    }
    onSubmit({ tg_id: tgId, tariff_id: tariffId, billing, note });
  };

  const tariffOptions = [
    { value: '', label: 'Select a tariff…' },
    ...tariffs.map((t) => ({
      value: String(t.id),
      label: `${t.name} (${t.period_days}d)`,
    })),
  ];

  return (
    <Modal isOpen={open} onClose={onClose} title="Grant tariff access">
      <div className="flex flex-col gap-4">
        <div className="flex flex-col gap-1.5">
          <span className="text-xs font-semibold uppercase tracking-wider text-white/55">
            Telegram ID
          </span>
          <TgIdCombo value={tgIdStr} onChange={setTgIdStr} users={botUsers} />
        </div>

        <div className="flex flex-col gap-1.5">
          <span className="text-xs font-semibold uppercase tracking-wider text-white/55">
            Tariff
          </span>
          <Select
            value={tariffId === null ? '' : String(tariffId)}
            onChange={(e) => setTariffId(e.target.value ? parseInt(e.target.value, 10) : null)}
            options={tariffOptions}
          />
        </div>

        <div className="flex flex-col gap-1.5">
          <span className="text-xs font-semibold uppercase tracking-wider text-white/55">
            Billing
          </span>
          <div className="inline-flex rounded-xl border border-white/[0.08] bg-black/30 p-1">
            <button
              type="button"
              onClick={() => setBilling('paid')}
              className={cn(
                'flex-1 rounded-lg px-3 py-2 text-sm font-semibold transition-all duration-200',
                billing === 'paid'
                  ? 'border border-violet-500/40 bg-gradient-to-br from-violet-500/30 to-violet-600/20 text-white shadow-[0_0_10px_rgba(208,188,255,0.15)]'
                  : 'border border-transparent text-white/55 hover:text-white/80'
              )}
            >
              Paid
            </button>
            <button
              type="button"
              onClick={() => setBilling('gift')}
              className={cn(
                'flex-1 rounded-lg px-3 py-2 text-sm font-semibold transition-all duration-200',
                billing === 'gift'
                  ? 'border border-amber-500/40 bg-gradient-to-br from-amber-500/30 to-amber-600/20 text-white shadow-[0_0_10px_rgba(251,191,36,0.15)]'
                  : 'border border-transparent text-white/55 hover:text-white/80'
              )}
            >
              Gift
            </button>
            <button
              type="button"
              onClick={() => setBilling('free')}
              className={cn(
                'flex-1 rounded-lg px-3 py-2 text-sm font-semibold transition-all duration-200',
                billing === 'free'
                  ? 'border border-emerald-500/40 bg-gradient-to-br from-emerald-500/30 to-emerald-600/20 text-white shadow-[0_0_10px_rgba(110,231,183,0.15)]'
                  : 'border border-transparent text-white/55 hover:text-white/80'
              )}
            >
              Free
            </button>
          </div>
          <p className="text-xs text-white/45">
            {billing === 'paid'
              ? 'Unlocks a private tariff for purchase. User still pays themselves.'
              : billing === 'gift'
                ? 'One free period of access as a gift. No auto-renewal — expires naturally.'
                : 'Lifetime access — auto-renewed by the system without payment.'}
          </p>
        </div>

        <label className="flex flex-col gap-1.5">
          <span className="text-xs font-semibold uppercase tracking-wider text-white/55">
            Note (optional)
          </span>
          <input
            type="text"
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="e.g. VIP friend, refund #4214"
            className="rounded-xl border border-white/[0.08] bg-black/40 px-3 py-2.5 text-sm text-white placeholder-white/30 transition-colors focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary/50"
          />
        </label>

        <div className="mt-2 flex justify-end gap-3">
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button onClick={handleSubmit} disabled={submitting}>
            {submitting ? 'Saving…' : 'Save'}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

export function GrantsTab() {
  const queryClient = useQueryClient();
  const [showAdd, setShowAdd] = useState(false);
  const [confirmRow, setConfirmRow] = useState<GrantRow | null>(null);

  const grantsQuery = useQuery({
    queryKey: ['bot', 'grants'],
    queryFn: listGrants,
  });
  const tariffsQuery = useQuery({
    queryKey: ['bot', 'tariffs'],
    queryFn: listTariffs,
  });
  const botUsersQuery = useQuery({
    queryKey: ['bot', 'users'],
    queryFn: listBotUsers,
    staleTime: 60_000,
  });

  const addMutation = useMutation({
    mutationFn: (args: { tg_id: number; tariff_id: number; billing: GrantBilling; note: string }) =>
      createGrant(args.tg_id, {
        tariff_id: args.tariff_id,
        billing: args.billing,
        note: args.note || undefined,
      }),
    onSuccess: (_data, vars) => {
      const label =
        vars.billing === 'free'
          ? 'Lifetime grant created'
          : vars.billing === 'gift'
            ? 'One-period gift granted'
            : 'Private-tariff access granted';
      toast.success(label);
      queryClient.invalidateQueries({ queryKey: ['bot', 'grants'] });
      queryClient.invalidateQueries({ queryKey: ['bot', 'users'] });
      queryClient.invalidateQueries({ queryKey: ['bot', 'user', vars.tg_id] });
      setShowAdd(false);
    },
    onError: (e: unknown) => {
      const msg =
        (e as { response?: { data?: { error?: string } } })?.response?.data?.error ?? 'Add failed';
      toast.error(msg);
    },
  });

  const revokeMutation = useMutation({
    mutationFn: (row: GrantRow) => revokeTariff(row.telegram_id, row.tariff_id),
    onSuccess: (data) => {
      toast.success(
        `Grant revoked. Disabled ${data.disabled_clients} client(s), removed ${data.revoked_grants} grant(s).`
      );
      queryClient.invalidateQueries({ queryKey: ['bot', 'grants'] });
      queryClient.invalidateQueries({ queryKey: ['bot', 'users'] });
      queryClient.invalidateQueries({ queryKey: ['bot', 'user'] });
      setConfirmRow(null);
    },
    onError: () => {
      toast.error('Remove failed');
      setConfirmRow(null);
    },
  });

  const confirmDescription = (() => {
    if (!confirmRow) return '';
    const who = confirmRow.username
      ? `@${confirmRow.username}`
      : `Telegram user ${confirmRow.telegram_id}`;
    return `Revoke "${confirmRow.tariff_name}" from ${who}? This disables every active client subscription for this tariff and removes the access grant. Past traffic statistics are preserved.`;
  })();

  if (grantsQuery.isLoading) return <p className="text-sm text-white/60">Loading…</p>;
  if (grantsQuery.error) return <p className="text-sm text-rose-400">Failed to load grants.</p>;

  const rows = grantsQuery.data || [];
  const tariffs = tariffsQuery.data || [];
  const botUsers = botUsersQuery.data || [];

  return (
    <div className="flex flex-col gap-4">
      <div className="flex justify-end">
        <button
          type="button"
          onClick={() => setShowAdd(true)}
          className="flex items-center gap-2 rounded-xl bg-primary/20 px-4 py-2 text-sm font-medium text-primary-100 transition-colors hover:bg-primary/30"
        >
          <Plus className="h-4 w-4" />
          New grant
        </button>
      </div>

      {rows.length === 0 ? (
        <div className="rounded-xl border border-white/[0.05] bg-white/[0.02] p-8 text-center text-sm text-white/60">
          No grants yet. Click "New grant" to give a user access to a tariff.
        </div>
      ) : (
        <div className="overflow-x-auto rounded-2xl border border-white/[0.05] bg-white/[0.02]">
          <table className="w-full text-sm whitespace-nowrap">
            <thead className="bg-black/40 text-left text-xs uppercase tracking-wider text-white/50">
              <tr>
                <th className="px-4 py-3 font-medium">TG ID</th>
                <th className="px-4 py-3 font-medium">Username</th>
                <th className="px-4 py-3 font-medium">Tariff</th>
                <th className="px-4 py-3 font-medium">Billing</th>
                <th className="px-4 py-3 font-medium">Renews</th>
                <th className="px-4 py-3 font-medium">Note</th>
                <th className="w-24 px-4 py-3 text-right font-medium">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-white/[0.04]">
              {rows.map((r) => (
                <tr key={r.id} className="transition-colors hover:bg-white/[0.04]">
                  <td className="px-4 py-3 font-mono text-white/90">{r.telegram_id}</td>
                  <td className="px-4 py-3 text-white/90">{r.username || '—'}</td>
                  <td className="px-4 py-3 text-white/90">{r.tariff_name}</td>
                  <td className="px-4 py-3">
                    <span
                      className={cn(
                        'rounded-md border px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wider',
                        billingChipClass(r.billing)
                      )}
                    >
                      {r.billing}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-white/60">{formatDate(r.next_renewal_at)}</td>
                  <td className="px-4 py-3 text-white/60">{r.note || '—'}</td>
                  <td className="px-4 py-3 text-right">
                    <button
                      type="button"
                      onClick={() => setConfirmRow(r)}
                      className="inline-flex items-center gap-1.5 rounded-lg border border-rose-500/40 bg-rose-500/10 px-3 py-1.5 text-xs font-semibold text-rose-300 shadow-[0_0_12px_rgba(244,63,94,0.15)] transition-all hover:border-rose-400/70 hover:bg-rose-500/25 hover:text-rose-100 hover:shadow-[0_0_18px_rgba(244,63,94,0.35)] active:scale-95"
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                      Revoke
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <AddDialog
        open={showAdd}
        tariffs={tariffs}
        botUsers={botUsers}
        onClose={() => setShowAdd(false)}
        onSubmit={(args) => addMutation.mutate(args)}
        submitting={addMutation.isPending}
      />

      <ConfirmationModal
        isOpen={confirmRow !== null}
        onClose={() => setConfirmRow(null)}
        onConfirm={() => {
          if (confirmRow) revokeMutation.mutate(confirmRow);
        }}
        title="Revoke grant"
        description={confirmDescription}
        confirmText="Revoke grant"
        isLoading={revokeMutation.isPending}
      />
    </div>
  );
}

function TgIdCombo({
  value,
  onChange,
  users,
}: {
  value: string;
  onChange: (v: string) => void;
  users: BotUser[];
}) {
  const [open, setOpen] = useState(false);
  const [dropdownStyle, setDropdownStyle] = useState<React.CSSProperties>({});
  const inputRef = useRef<HTMLInputElement>(null);
  const dropdownRef = useRef<HTMLDivElement>(null);

  const filtered = useMemo(() => {
    const needle = value.trim().toLowerCase().replace(/^@/, '');
    if (!needle) return users.slice(0, 30);
    return users
      .filter(
        (u) =>
          String(u.telegram_id).includes(needle) ||
          (u.username && u.username.toLowerCase().includes(needle))
      )
      .slice(0, 50);
  }, [users, value]);

  const matched = useMemo(
    () => users.find((u) => String(u.telegram_id) === value.trim()),
    [users, value]
  );

  const updatePosition = () => {
    if (!inputRef.current) return;
    const rect = inputRef.current.getBoundingClientRect();
    const dropdownH = Math.min(filtered.length * 44 + 8, 280);
    const spaceBelow = window.innerHeight - rect.bottom;
    const openUpward = spaceBelow < dropdownH + 8 && rect.top > dropdownH;
    setDropdownStyle({
      position: 'fixed',
      left: rect.left,
      width: rect.width,
      zIndex: 10000,
      ...(openUpward ? { bottom: window.innerHeight - rect.top + 6 } : { top: rect.bottom + 6 }),
    });
  };

  useEffect(() => {
    if (!open) return;
    updatePosition();
    const onScroll = () => updatePosition();
    window.addEventListener('scroll', onScroll, true);
    window.addEventListener('resize', onScroll);
    return () => {
      window.removeEventListener('scroll', onScroll, true);
      window.removeEventListener('resize', onScroll);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, filtered.length]);

  useEffect(() => {
    if (!open) return;
    const onMouse = (e: MouseEvent) => {
      if (
        !inputRef.current?.contains(e.target as Node) &&
        !dropdownRef.current?.contains(e.target as Node)
      ) {
        setOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onMouse);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onMouse);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  return (
    <div className="relative">
      <input
        ref={inputRef}
        type="text"
        inputMode="numeric"
        value={value}
        onChange={(e) => {
          onChange(e.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        placeholder="Search by username or paste Telegram ID"
        className="w-full rounded-xl border border-white/[0.08] bg-black/40 px-3 py-2.5 text-sm text-white placeholder-white/30 transition-colors focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary/50"
      />
      {matched && (
        <p className="mt-1.5 text-xs text-emerald-300/80">
          ✓ {matched.username ? `@${matched.username}` : 'no username'} ·{' '}
          <span className="font-mono">{matched.telegram_id}</span>
        </p>
      )}
      {open &&
        filtered.length > 0 &&
        createPortal(
          <div
            ref={dropdownRef}
            style={dropdownStyle}
            className="max-h-[280px] overflow-y-auto rounded-xl border border-white/[0.10] bg-zinc-950 shadow-2xl"
          >
            {filtered.map((u) => {
              const isSelected = String(u.telegram_id) === value.trim();
              return (
                <button
                  key={u.telegram_id}
                  type="button"
                  onMouseDown={(e) => e.preventDefault()}
                  onClick={() => {
                    onChange(String(u.telegram_id));
                    setOpen(false);
                  }}
                  className={cn(
                    'flex w-full items-center justify-between gap-3 border-b border-white/[0.04] px-3 py-2.5 text-left text-sm transition-colors last:border-b-0',
                    isSelected ? 'bg-violet-500/10' : 'hover:bg-white/[0.05]'
                  )}
                >
                  <span className="flex items-center gap-2 truncate text-white/90">
                    {u.username ? (
                      <span className="truncate">@{u.username}</span>
                    ) : (
                      <span className="italic text-white/40">no username</span>
                    )}
                    {u.blocked && (
                      <span className="rounded border border-rose-500/30 bg-rose-500/10 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wider text-rose-300">
                        blocked
                      </span>
                    )}
                  </span>
                  <span className="font-mono text-xs text-white/45">{u.telegram_id}</span>
                </button>
              );
            })}
          </div>,
          document.body
        )}
    </div>
  );
}
