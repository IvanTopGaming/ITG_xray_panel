import { useEffect, useMemo, useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { X, Plus, Trash2, Package, Gift } from 'lucide-react';
import { cn } from '@/lib/utils';
import { Select } from '@/components/ui/Select';
import { TagInput } from '@/components/ui/TagInput';
import { ConfirmationModal } from '@/components/ui/ConfirmationModal';
import type {
  Inbound,
  Tariff,
  TariffStats,
  TariffVisibility,
  TariffWritePayload,
} from '@/lib/types';

interface TariffDrawerProps {
  open: boolean;
  tariff: Tariff | null; // null = create mode
  stats: TariffStats | null;
  inbounds: Inbound[];
  nodeGroups: string[];
  saving: boolean;
  onClose: () => void;
  onSave: (payload: TariffWritePayload) => Promise<void>;
}

interface FormItem {
  inbound_tag: string;
  label: string;
  traffic_gb: string;
  allowed_node_groups: string;
  sort_order: number;
}

interface FormState {
  name: string;
  price_rub: string;
  period_days: string;
  visibility: TariffVisibility;
  is_trial: boolean;
  enabled: boolean;
  sort_order: string;
  items: FormItem[];
}

const emptyForm = (isTrial = false): FormState => ({
  name: '',
  price_rub: isTrial ? '0' : '0',
  period_days: isTrial ? '1' : '30',
  visibility: 'public',
  is_trial: isTrial,
  enabled: true,
  sort_order: '0',
  items: [
    {
      inbound_tag: '',
      label: '',
      traffic_gb: '0',
      allowed_node_groups: '',
      sort_order: 0,
    },
  ],
});

const tariffToForm = (t: Tariff): FormState => ({
  name: t.name,
  price_rub: String(t.price_rub),
  period_days: String(t.period_days),
  visibility: t.visibility,
  is_trial: t.is_trial,
  enabled: t.enabled,
  sort_order: String(t.sort_order),
  items: t.items.map((i) => ({
    inbound_tag: i.inbound_tag,
    label: i.label,
    traffic_gb: String(i.traffic_gb),
    allowed_node_groups: i.allowed_node_groups,
    sort_order: i.sort_order,
  })),
});

export function TariffDrawer({
  open,
  tariff,
  stats,
  inbounds,
  nodeGroups,
  saving,
  onClose,
  onSave,
}: TariffDrawerProps) {
  const [form, setForm] = useState<FormState>(emptyForm());
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setForm(tariff ? tariffToForm(tariff) : emptyForm());
    setError(null);
  }, [open, tariff]);

  useEffect(() => {
    if (!open) return;
    const onEsc = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', onEsc);
    return () => document.removeEventListener('keydown', onEsc);
  }, [open, onClose]);

  const updateItem = (idx: number, key: keyof FormItem, value: string) => {
    setForm((prev) => ({
      ...prev,
      items: prev.items.map((it, i) => (i === idx ? { ...it, [key]: value } : it)),
    }));
  };

  const addItem = () => {
    setForm((prev) => ({
      ...prev,
      items: [
        ...prev.items,
        {
          inbound_tag: '',
          label: '',
          traffic_gb: '0',
          allowed_node_groups: '',
          sort_order: prev.items.length,
        },
      ],
    }));
  };

  const removeItem = (idx: number) => {
    setForm((prev) => ({ ...prev, items: prev.items.filter((_, i) => i !== idx) }));
  };

  const handleSubmit = async () => {
    setError(null);
    const payload: TariffWritePayload = {
      name: form.name.trim(),
      price_rub: parseInt(form.price_rub, 10) || 0,
      period_days: parseInt(form.period_days, 10) || 0,
      visibility: form.visibility,
      is_trial: form.is_trial,
      enabled: form.enabled,
      sort_order: parseInt(form.sort_order, 10) || 0,
      items: form.items
        .filter((it) => it.inbound_tag.trim())
        .map((it) => ({
          inbound_tag: it.inbound_tag.trim(),
          label: it.label.trim(),
          traffic_gb: parseInt(it.traffic_gb, 10) || 0,
          allowed_node_groups: it.allowed_node_groups.trim(),
          sort_order: it.sort_order,
        })),
    };
    if (!payload.name) {
      setError('Name is required.');
      return;
    }
    if (payload.period_days <= 0) {
      setError('Period must be > 0 days.');
      return;
    }
    if (payload.items.length === 0) {
      setError('At least one inbound is required.');
      return;
    }
    try {
      await onSave(payload);
      onClose();
    } catch (e) {
      const err = e as { response?: { data?: { error?: string } } };
      setError(err?.response?.data?.error || 'Save failed.');
    }
  };

  const isCreate = tariff === null;

  return (
    <AnimatePresence>
      {open && (
        <>
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.15 }}
            className="fixed inset-0 z-40 bg-black/55"
            onClick={onClose}
          />
          <motion.aside
            initial={{ x: '100%' }}
            animate={{ x: 0 }}
            exit={{ x: '100%' }}
            transition={{ type: 'spring', stiffness: 380, damping: 38 }}
            className="fixed right-0 top-0 z-50 flex h-screen w-full flex-col border-l border-white/[0.08] bg-zinc-950/95 shadow-2xl backdrop-blur md:w-[50vw] md:min-w-[600px] md:max-w-[920px]"
          >
            <DrawerHeader
              form={form}
              isCreate={isCreate}
              tariff={tariff}
              stats={stats}
              onClose={onClose}
            />

            <div className="flex-1 overflow-y-auto px-5 py-4">
              <SectionLabel>Basics</SectionLabel>
              <div className="space-y-4">
                <Field
                  label="Name"
                  value={form.name}
                  onChange={(v) => setForm({ ...form, name: v })}
                  placeholder="Premium 180d"
                  description="Shown to users at the top of each catalog card. Keep it short."
                />
                <div className="grid grid-cols-2 gap-4">
                  <Field
                    label="Price (₽)"
                    value={form.price_rub}
                    onChange={(v) => setForm({ ...form, price_rub: v.replace(/\D/g, '') })}
                    type="number"
                    description="In rubles. 0 = free tariff (no payment step)."
                  />
                  <Field
                    label="Period (days)"
                    value={form.period_days}
                    onChange={(v) => setForm({ ...form, period_days: v.replace(/\D/g, '') })}
                    type="number"
                    description="How long access lasts after purchase or renewal."
                  />
                </div>
                <div className="grid grid-cols-2 gap-4">
                  <div className="block">
                    <div className="mb-1 flex items-baseline justify-between">
                      <span className="text-xs font-semibold uppercase tracking-wider text-white/65">
                        Visibility
                      </span>
                      {form.is_trial && (
                        <span className="text-xs text-white/55">Trial is always public</span>
                      )}
                    </div>
                    <Select
                      value={form.visibility}
                      disabled={form.is_trial}
                      onChange={(e) =>
                        setForm({ ...form, visibility: e.target.value as TariffVisibility })
                      }
                      options={[
                        { value: 'public', label: 'Public' },
                        { value: 'private', label: 'Private (by invite)' },
                        { value: 'archived', label: 'Archived' },
                      ]}
                    />
                    <p className="mt-2 text-sm leading-relaxed text-white/55">
                      {
                        {
                          public: 'Visible to every bot user in the catalog.',
                          private:
                            'Hidden from the catalog — visible only to users granted explicit access.',
                          archived:
                            'Hidden everywhere. Existing subscriptions keep working; no new purchases.',
                        }[form.visibility]
                      }
                    </p>
                  </div>
                  <Field
                    label="Sort order"
                    value={form.sort_order}
                    onChange={(v) => setForm({ ...form, sort_order: v.replace(/\D/g, '') })}
                    type="number"
                    description="Lower numbers appear first in the bot catalog."
                  />
                </div>
                <div className="flex flex-wrap items-center gap-4 pt-1">
                  <ToggleField
                    label="Enabled"
                    value={form.enabled}
                    onChange={(v) => setForm({ ...form, enabled: v })}
                    hint={
                      form.is_trial
                        ? 'Off to stop offering trial to new users'
                        : 'Off to take it down without archiving'
                    }
                  />
                  {form.is_trial && (
                    <span className="inline-flex items-center gap-1.5 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-1.5 text-xs font-semibold uppercase tracking-wider text-amber-300">
                      🎁 Trial tariff
                    </span>
                  )}
                </div>
              </div>

              <SectionLabel className="mt-6">
                Includes
                <span className="ml-1.5 text-white/40">
                  ({form.items.length} inbound{form.items.length === 1 ? '' : 's'})
                </span>
              </SectionLabel>
              <p className="-mt-1.5 mb-3 text-sm leading-relaxed text-white/55">
                Each line provisions one inbound for the user when they buy this tariff. Add several
                for multi-protocol bundles.
              </p>
              <div className="space-y-3">
                {form.items.map((item, idx) => (
                  <ItemRow
                    key={idx}
                    item={item}
                    inbounds={inbounds}
                    nodeGroups={nodeGroups}
                    onChange={(key, value) => updateItem(idx, key, value)}
                    onRemove={() => removeItem(idx)}
                  />
                ))}
                <button
                  type="button"
                  onClick={addItem}
                  className="flex w-full items-center justify-center gap-2 rounded-xl border border-dashed border-white/[0.10] bg-white/[0.02] py-4 text-base font-medium text-white/65 transition-colors hover:border-violet-500/30 hover:bg-violet-500/[0.05] hover:text-violet-200"
                >
                  <Plus size={16} />
                  Add inbound
                </button>
              </div>
            </div>

            <div className="border-t border-white/[0.06] bg-zinc-950/95 px-5 py-3">
              {error && (
                <div className="mb-3 rounded-lg border border-rose-500/25 bg-rose-500/10 px-3 py-2.5 text-sm text-rose-200">
                  {error}
                </div>
              )}
              <div className="flex gap-3">
                <button
                  type="button"
                  onClick={onClose}
                  className="flex-1 rounded-xl border border-white/[0.06] bg-white/[0.04] px-5 py-3.5 text-base font-medium text-white/80 hover:bg-white/[0.08] hover:text-white"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  onClick={handleSubmit}
                  disabled={saving}
                  className="flex-1 rounded-xl border border-violet-500/40 bg-gradient-to-br from-violet-500/30 to-violet-600/25 px-5 py-3.5 text-base font-bold text-white shadow-[0_0_12px_rgba(168,85,247,0.20)] transition-colors hover:from-violet-500/40 disabled:opacity-50"
                >
                  {saving ? 'Saving…' : isCreate ? 'Create tariff' : 'Save changes'}
                </button>
              </div>
            </div>
          </motion.aside>
        </>
      )}
    </AnimatePresence>
  );
}

function DrawerHeader({
  form,
  isCreate,
  tariff,
  stats,
  onClose,
}: {
  form: FormState;
  isCreate: boolean;
  tariff: Tariff | null;
  stats: TariffStats | null;
  onClose: () => void;
}) {
  const Icon = form.is_trial ? Gift : Package;
  const accent = form.is_trial
    ? 'border-amber-500/25 bg-amber-500/15 text-amber-300'
    : 'border-violet-500/25 bg-violet-500/15 text-violet-200';

  return (
    <div className="flex items-start justify-between border-b border-white/[0.06] px-5 py-4">
      <div className="flex min-w-0 items-center gap-3">
        <div
          className={cn(
            'flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border',
            accent
          )}
        >
          <Icon size={16} />
        </div>
        <div className="min-w-0">
          <div className="truncate text-lg font-semibold text-white">
            {isCreate ? 'New tariff' : form.name || 'Untitled'}
          </div>
          {!isCreate && tariff && stats && (
            <div className="mt-1 flex items-center gap-2 text-sm text-white/60">
              <span>
                <span className="text-violet-300 font-semibold">{stats.active_subs}</span> active
              </span>
              <span>·</span>
              <span>
                <span className="text-emerald-300 font-semibold">{stats.revenue_30d}₽</span> in 30d
              </span>
            </div>
          )}
        </div>
      </div>
      <button
        type="button"
        onClick={onClose}
        className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg border border-white/[0.06] bg-white/[0.04] text-white/55 hover:bg-white/[0.10] hover:text-white"
      >
        <X size={14} />
      </button>
    </div>
  );
}

function SectionLabel({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <div
      className={cn(
        'mb-4 flex items-baseline gap-2 border-l-2 border-violet-500/50 pl-2.5 text-lg font-semibold text-white',
        className
      )}
    >
      {children}
    </div>
  );
}

interface FieldProps {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  type?: 'text' | 'number';
  multiline?: boolean;
  hint?: string;
  description?: string;
}

function Field({
  label,
  value,
  onChange,
  placeholder,
  type = 'text',
  multiline,
  hint,
  description,
}: FieldProps) {
  return (
    <label className="block">
      <div className="mb-1 flex items-baseline justify-between">
        <span className="text-xs font-semibold uppercase tracking-wider text-white/65">
          {label}
        </span>
        {hint && <span className="text-xs text-white/55">{hint}</span>}
      </div>
      {multiline ? (
        <textarea
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder}
          rows={2}
          className="w-full resize-none rounded-xl border border-white/[0.06] bg-white/[0.04] px-3 py-2 text-sm text-white placeholder:text-white/25 focus:border-violet-500/40 focus:outline-none"
        />
      ) : (
        <input
          type={type}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder}
          className="w-full rounded-xl border border-white/[0.06] bg-white/[0.04] px-3.5 py-3 text-base text-white placeholder:text-white/30 focus:border-violet-500/40 focus:outline-none"
        />
      )}
      {description && <p className="mt-2 text-sm leading-relaxed text-white/55">{description}</p>}
    </label>
  );
}

function ToggleField({
  label,
  value,
  onChange,
  hint,
}: {
  label: string;
  value: boolean;
  onChange: (v: boolean) => void;
  hint?: string;
}) {
  return (
    <label className="flex cursor-pointer items-center gap-2">
      <button
        type="button"
        onClick={() => onChange(!value)}
        className={cn(
          'flex h-6 w-11 items-center rounded-full p-0.5 transition-colors',
          value ? 'bg-violet-500/50' : 'bg-white/[0.08]'
        )}
      >
        <span
          className={cn(
            'h-5 w-5 rounded-full bg-white transition-transform',
            value ? 'translate-x-5' : 'translate-x-0'
          )}
        />
      </button>
      <div className="flex flex-col">
        <span className="text-base text-white/95">{label}</span>
        {hint && <span className="text-sm text-white/55">{hint}</span>}
      </div>
    </label>
  );
}

function ItemRow({
  item,
  inbounds,
  nodeGroups,
  onChange,
  onRemove,
}: {
  item: FormItem;
  inbounds: Inbound[];
  nodeGroups: string[];
  onChange: (key: keyof FormItem, value: string) => void;
  onRemove: () => void;
}) {
  const matchedInbound = useMemo(
    () => inbounds.find((i) => i.tag === item.inbound_tag) || null,
    [inbounds, item.inbound_tag]
  );
  const [confirmRemove, setConfirmRemove] = useState(false);
  const itemLabel = item.inbound_tag.trim() || 'this inbound';

  const inboundOptions = useMemo(
    () => [
      { value: '', label: 'Pick an inbound…' },
      ...inbounds.map((i) => ({
        value: i.tag,
        label: `${i.tag}  ·  ${i.protocol} :${i.port}`,
      })),
    ],
    [inbounds]
  );

  const selectedGroups = useMemo(
    () =>
      item.allowed_node_groups
        .split(',')
        .map((s) => s.trim())
        .filter(Boolean),
    [item.allowed_node_groups]
  );

  const isUnknown = !!item.inbound_tag.trim() && !matchedInbound;

  return (
    <div className="rounded-xl border border-white/[0.06] bg-white/[0.03] p-4">
      <div className="mb-3 flex items-center justify-between">
        <span className="text-xs font-semibold uppercase tracking-wider text-violet-300">
          Inbound
        </span>
        <button
          type="button"
          onClick={() => {
            if (!item.inbound_tag.trim()) {
              onRemove();
            } else {
              setConfirmRemove(true);
            }
          }}
          className="inline-flex items-center gap-1.5 rounded-lg border border-rose-500/25 bg-rose-500/10 px-2.5 py-1.5 text-xs font-semibold text-rose-300 transition-colors hover:border-rose-500/40 hover:bg-rose-500/15 hover:text-rose-200"
          aria-label="Remove inbound"
        >
          <Trash2 size={14} />
          Remove
        </button>
      </div>
      <ConfirmationModal
        isOpen={confirmRemove}
        onClose={() => setConfirmRemove(false)}
        onConfirm={() => {
          onRemove();
          setConfirmRemove(false);
        }}
        title="Remove inbound"
        description={`Remove "${itemLabel}" from this tariff? Existing subscribers keep their access; this only affects future purchases.`}
        confirmText="Remove"
        confirmVariant="danger"
      />

      <div className="space-y-3">
        <div>
          <Select
            value={item.inbound_tag}
            onChange={(e) => onChange('inbound_tag', e.target.value)}
            options={inboundOptions}
          />
          <p
            className={cn(
              'mt-2 text-sm leading-relaxed',
              isUnknown ? 'text-amber-300/90' : 'text-white/55'
            )}
          >
            {matchedInbound
              ? `✓ matches ${matchedInbound.protocol} :${matchedInbound.port}`
              : isUnknown
                ? `⚠ no inbound with this tag — provisioning will fail`
                : 'Pick from existing panel inbounds.'}
          </p>
        </div>

        <div className="grid grid-cols-2 gap-3">
          <div>
            <span className="mb-1 block text-xs font-semibold uppercase tracking-wider text-white/65">
              Traffic GB
            </span>
            <input
              type="number"
              value={item.traffic_gb}
              onChange={(e) => onChange('traffic_gb', e.target.value.replace(/\D/g, ''))}
              placeholder="0"
              className="w-full rounded-lg border border-white/[0.06] bg-black/30 px-3.5 py-3 font-mono text-base text-white placeholder:text-white/25 focus:border-violet-500/40 focus:outline-none"
            />
            <p className="mt-2 text-sm leading-relaxed text-white/55">
              <span className="text-emerald-300/90">0 = unlimited.</span>
            </p>
          </div>
          <div>
            <TagInput
              label="Node groups"
              value={selectedGroups}
              onChange={(tags) => onChange('allowed_node_groups', tags.join(','))}
              suggestions={nodeGroups}
              placeholder={selectedGroups.length === 0 ? 'Any node…' : ''}
              helperText="Empty = provision on any node."
              pattern={null}
              maxLength={40}
            />
          </div>
        </div>

        <div>
          <span className="mb-1 block text-xs font-semibold uppercase tracking-wider text-white/65">
            Label
          </span>
          <input
            value={item.label}
            onChange={(e) => onChange('label', e.target.value)}
            placeholder={
              matchedInbound ? `e.g. ${matchedInbound.tag} · 100 GB` : 'VLESS Premium · unlimited'
            }
            className="w-full rounded-lg border border-white/[0.06] bg-black/30 px-3.5 py-3 text-base text-white placeholder:text-white/25 focus:border-violet-500/40 focus:outline-none"
          />
          <p className="mt-2 text-sm leading-relaxed text-white/55">
            What users see for this line in the bot. Optional — defaults to inbound tag.
          </p>
        </div>
      </div>
    </div>
  );
}
