import { useEffect, useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Eye, EyeOff, Copy, Check, RefreshCw } from 'lucide-react';
import { getBotSettings, updateBotSettings, rotateBotServiceToken } from '../../lib/bot';
import type { BotSettingsUpdate } from '@ui/lib/types';
import { ConfirmationModal } from '@ui/components/ui/ConfirmationModal';
import { Switch } from '@ui/components/ui/Switch';

export function SettingsTab() {
  const qc = useQueryClient();
  const settings = useQuery({ queryKey: ['bot-settings'], queryFn: getBotSettings });
  const [draft, setDraft] = useState({
    yookassa_shop_id: '',
    yookassa_secret_key: '',
    yookassa_return_url: '',
  });
  const [botDraft, setBotDraft] = useState({
    bot_token: '',
    telegram_proxy_url: '',
    admin_ids_text: '',
    display_timezone: '',
  });
  const [deviceDraft, setDeviceDraft] = useState({ enabled: false, perUser: 0 });
  const [savedAt, setSavedAt] = useState<number | null>(null);
  const [botSavedAt, setBotSavedAt] = useState<number | null>(null);
  const [deviceSavedAt, setDeviceSavedAt] = useState<number | null>(null);
  const [rotatedAt, setRotatedAt] = useState<number | null>(null);
  const [rotateConfirmOpen, setRotateConfirmOpen] = useState(false);

  const data = settings.data;

  useEffect(() => {
    if (!data) return;
    setDraft({
      yookassa_shop_id: data.yookassa_shop_id || '',
      yookassa_secret_key: data.yookassa_secret_key || '',
      yookassa_return_url: data.yookassa_return_url || '',
    });
    setBotDraft({
      bot_token: data.bot_token || '',
      telegram_proxy_url: data.telegram_proxy_url || '',
      admin_ids_text: data.admin_ids?.join(', ') ?? '',
      display_timezone: data.display_timezone || 'Europe/Moscow',
    });
    setDeviceDraft({
      enabled: !!data.device_limit_enabled,
      perUser: Number(data.device_limit_per_user ?? 0),
    });
  }, [data?.bot_config_version, data]);

  const save = useMutation({
    mutationFn: () => updateBotSettings(draft),
    onSuccess: () => {
      setSavedAt(Date.now());
      qc.invalidateQueries({ queryKey: ['bot-settings'] });
    },
  });

  const parseAdminIds = (text: string): number[] => {
    return text
      .split(/[,;\s]+/)
      .map((s) => s.trim())
      .filter(Boolean)
      .map((s) => Number(s))
      .filter((n) => Number.isFinite(n) && Number.isInteger(n));
  };

  const saveBot = useMutation({
    mutationFn: () => {
      const payload: BotSettingsUpdate = {
        admin_ids: parseAdminIds(botDraft.admin_ids_text),
        telegram_proxy_url: botDraft.telegram_proxy_url,
        display_timezone: botDraft.display_timezone || 'Europe/Moscow',
        bot_token: botDraft.bot_token,
      };
      return updateBotSettings(payload);
    },
    onSuccess: () => {
      setBotSavedAt(Date.now());
      qc.invalidateQueries({ queryKey: ['bot-settings'] });
    },
  });

  const saveDevices = useMutation({
    mutationFn: () =>
      updateBotSettings({
        device_limit_enabled: deviceDraft.enabled,
        device_limit_per_user: Number(deviceDraft.perUser) || 0,
      }),
    onSuccess: () => {
      setDeviceSavedAt(Date.now());
      qc.invalidateQueries({ queryKey: ['bot-settings'] });
    },
  });

  const rotate = useMutation({
    mutationFn: rotateBotServiceToken,
    onSuccess: () => {
      setRotatedAt(Date.now());
      setRotateConfirmOpen(false);
      qc.invalidateQueries({ queryKey: ['bot-settings'] });
    },
  });

  if (!data) return <div className="text-white/50">Loading…</div>;

  return (
    <>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6 items-start">
        <div className="space-y-6">
          <section className="relative overflow-hidden rounded-2xl border border-white/[0.05] bg-gradient-to-br from-white/[0.04] to-white/[0.01] p-6 shadow-sm space-y-4">
            <div className="flex items-baseline justify-between gap-3">
              <h3 className="text-white font-semibold">Telegram Bot</h3>
              <span className="text-xs text-white/40">
                v{data.bot_config_version} ·{' '}
                {data.has_bot_token ? (
                  <span className="text-emerald-300">configured</span>
                ) : (
                  <span className="text-amber-300">not set — bot waits for token</span>
                )}
              </span>
            </div>
            <p className="text-white/60 text-xs">
              Bot fetches these on startup and re-polls every 30s. Changing the bot token or proxy
              gracefully restarts the polling session in-place — no container restart needed.
            </p>
            <div className="space-y-3">
              <SecretField
                label="Bot token"
                value={botDraft.bot_token}
                placeholder="123456789:ABCdef…"
                onChange={(v) => setBotDraft({ ...botDraft, bot_token: v })}
              />
              <Field
                label="Admin Telegram IDs (comma-separated)"
                value={botDraft.admin_ids_text}
                placeholder="123456789, 987654321"
                onChange={(v) => setBotDraft({ ...botDraft, admin_ids_text: v })}
              />
              <Field
                label="Telegram proxy URL (optional)"
                value={botDraft.telegram_proxy_url}
                placeholder="socks5://user:pass@host:1080"
                onChange={(v) => setBotDraft({ ...botDraft, telegram_proxy_url: v })}
              />
              <Field
                label="Display timezone (IANA, e.g. Europe/Moscow)"
                value={botDraft.display_timezone}
                placeholder="Europe/Moscow"
                onChange={(v) => setBotDraft({ ...botDraft, display_timezone: v })}
              />
            </div>
            <div className="flex items-center gap-3">
              <button
                onClick={() => saveBot.mutate()}
                disabled={saveBot.isPending}
                className="rounded-xl bg-primary/20 px-4 py-2.5 text-sm font-medium text-primary-100 transition-colors hover:bg-primary/30 focus:outline-none focus:ring-2 focus:ring-primary/50 disabled:opacity-50"
              >
                {saveBot.isPending ? 'Saving…' : 'Save'}
              </button>
              {botSavedAt && Date.now() - botSavedAt < 3000 && (
                <span className="text-emerald-300 text-sm">Saved.</span>
              )}
            </div>
          </section>

          <section className="relative overflow-hidden rounded-2xl border border-white/[0.05] bg-gradient-to-br from-white/[0.04] to-white/[0.01] p-6 shadow-sm space-y-4">
            <h3 className="text-white font-semibold">Subscriptions · devices</h3>
            <p className="text-white/60 text-xs">
              Per-subscription device limit, counted by unique HWIDs across the user's whole
              subscription. Off by default — devices aren't limited and the device card on the
              subscription page is hidden.
            </p>
            <div className="space-y-3">
              <Switch
                checked={deviceDraft.enabled}
                onChange={(e) => setDeviceDraft((d) => ({ ...d, enabled: e.target.checked }))}
                label="Limit devices per subscription"
              />
              {deviceDraft.enabled && (
                <Field
                  label="Device limit (0 = unlimited)"
                  value={String(deviceDraft.perUser)}
                  onChange={(v) =>
                    setDeviceDraft((d) => ({ ...d, perUser: Number(v.replace(/\D/g, '')) || 0 }))
                  }
                />
              )}
            </div>
            <div className="flex items-center gap-3">
              <button
                onClick={() => saveDevices.mutate()}
                disabled={saveDevices.isPending}
                className="rounded-xl bg-primary/20 px-4 py-2.5 text-sm font-medium text-primary-100 transition-colors hover:bg-primary/30 focus:outline-none focus:ring-2 focus:ring-primary/50 disabled:opacity-50"
              >
                {saveDevices.isPending ? 'Saving…' : 'Save'}
              </button>
              {deviceSavedAt && Date.now() - deviceSavedAt < 3000 && (
                <span className="text-emerald-300 text-sm">Saved.</span>
              )}
            </div>
          </section>
        </div>
        <div className="space-y-6">
          <section className="relative overflow-hidden rounded-2xl border border-white/[0.05] bg-gradient-to-br from-white/[0.04] to-white/[0.01] p-6 shadow-sm space-y-4">
            <h3 className="text-white font-semibold">YooKassa</h3>
            <div className="space-y-3">
              <Field
                label="Shop ID"
                value={draft.yookassa_shop_id}
                onChange={(v) => setDraft({ ...draft, yookassa_shop_id: v })}
              />
              <SecretField
                label="Secret key"
                value={draft.yookassa_secret_key}
                onChange={(v) => setDraft({ ...draft, yookassa_secret_key: v })}
              />
              <Field
                label="Return URL"
                value={draft.yookassa_return_url}
                placeholder="https://t.me/your_bot?start=paid"
                onChange={(v) => setDraft({ ...draft, yookassa_return_url: v })}
              />
            </div>
            <div className="flex items-center gap-3">
              <button
                onClick={() => save.mutate()}
                disabled={save.isPending}
                className="rounded-xl bg-primary/20 px-4 py-2.5 text-sm font-medium text-primary-100 transition-colors hover:bg-primary/30 focus:outline-none focus:ring-2 focus:ring-primary/50 disabled:opacity-50"
              >
                {save.isPending ? 'Saving…' : 'Save'}
              </button>
              {savedAt && Date.now() - savedAt < 3000 && (
                <span className="text-emerald-300 text-sm">Saved.</span>
              )}
            </div>
          </section>

          <section className="relative overflow-hidden rounded-2xl border border-white/[0.05] bg-gradient-to-br from-white/[0.04] to-white/[0.01] p-6 shadow-sm space-y-3">
            <div className="flex items-baseline justify-between gap-3">
              <h3 className="text-white font-semibold">Bot service token</h3>
              <span className="text-xs text-white/40">
                {data.has_bot_service_token ? (
                  <span className="text-emerald-300">configured</span>
                ) : (
                  <span className="text-amber-300">not set</span>
                )}
              </span>
            </div>
            <p className="text-white/60 text-sm">
              The bot uses this token to call backend endpoints. Regenerating invalidates the old
              one immediately — update <code>BOT_SERVICE_TOKEN</code> in the bot config after
              rotation.
            </p>
            <SecretView value={data.bot_service_token || ''} placeholder="not set" />
            <div className="flex items-center gap-3">
              <button
                onClick={() =>
                  data.has_bot_service_token ? setRotateConfirmOpen(true) : rotate.mutate()
                }
                disabled={rotate.isPending}
                className="inline-flex items-center gap-2 rounded-xl bg-rose-500/20 px-4 py-2.5 text-sm font-medium text-rose-200 transition-colors hover:bg-rose-500/30 focus:outline-none focus:ring-2 focus:ring-rose-500/50 disabled:opacity-50"
              >
                <RefreshCw className={`h-4 w-4 ${rotate.isPending ? 'animate-spin' : ''}`} />
                {rotate.isPending
                  ? 'Regenerating…'
                  : data.has_bot_service_token
                    ? 'Regenerate token'
                    : 'Generate token'}
              </button>
              {rotatedAt && Date.now() - rotatedAt < 5000 && (
                <span className="text-emerald-300 text-sm">
                  New token generated — copy it and update the bot config.
                </span>
              )}
            </div>
          </section>
        </div>
      </div>

      <ConfirmationModal
        isOpen={rotateConfirmOpen}
        onClose={() => setRotateConfirmOpen(false)}
        onConfirm={() => rotate.mutate()}
        title="Regenerate bot service token"
        description="The current token will be invalidated immediately. The bot will stop calling the backend until you copy the new token into BOT_SERVICE_TOKEN in the bot config. Continue?"
        confirmText="Regenerate"
        confirmVariant="danger"
        isLoading={rotate.isPending}
      />
    </>
  );
}

function Field({
  label,
  value,
  onChange,
  type = 'text',
  placeholder,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  type?: 'text' | 'password';
  placeholder?: string;
}) {
  return (
    <label className="block">
      <span className="mb-1.5 block text-xs uppercase tracking-wider text-white/50">{label}</span>
      <input
        type={type}
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        className="w-full rounded-xl border border-white/[0.08] bg-black/40 px-3 py-2.5 text-sm text-white placeholder-white/30 transition-colors focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary/50"
      />
    </label>
  );
}

function SecretField({
  label,
  value,
  onChange,
  placeholder,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
}) {
  const [shown, setShown] = useState(false);
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    if (!value) return;
    await navigator.clipboard.writeText(value);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1500);
  };

  return (
    <label className="block">
      <span className="mb-1.5 block text-xs uppercase tracking-wider text-white/50">{label}</span>
      <div className="relative">
        <input
          type={shown ? 'text' : 'password'}
          value={value}
          placeholder={placeholder}
          onChange={(e) => onChange(e.target.value)}
          className="w-full rounded-xl border border-white/[0.08] bg-black/40 py-2.5 pl-3 pr-20 text-sm text-white placeholder-white/30 transition-colors focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary/50"
        />
        <div className="absolute inset-y-0 right-1 flex items-center gap-0.5">
          <button
            type="button"
            onClick={handleCopy}
            disabled={!value}
            title="Copy"
            className="rounded-lg p-1.5 text-white/50 transition-colors hover:bg-white/[0.06] hover:text-white/90 disabled:cursor-not-allowed disabled:opacity-30"
          >
            {copied ? <Check className="h-4 w-4 text-emerald-300" /> : <Copy className="h-4 w-4" />}
          </button>
          <button
            type="button"
            onClick={() => setShown((s) => !s)}
            title={shown ? 'Hide' : 'Show'}
            className="rounded-lg p-1.5 text-white/50 transition-colors hover:bg-white/[0.06] hover:text-white/90"
          >
            {shown ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
          </button>
        </div>
      </div>
    </label>
  );
}

function SecretView({ value, placeholder }: { value: string; placeholder?: string }) {
  const [shown, setShown] = useState(false);
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    if (!value) return;
    await navigator.clipboard.writeText(value);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1500);
  };

  const display = value ? (shown ? value : '•'.repeat(Math.min(value.length, 40))) : '';

  return (
    <div className="flex items-center gap-2 rounded-xl border border-white/[0.08] bg-black/40 py-2.5 pl-3 pr-1">
      <div className="flex-1 truncate font-mono text-sm text-white/90">
        {display || <span className="text-white/30">{placeholder || '—'}</span>}
      </div>
      <button
        type="button"
        onClick={handleCopy}
        disabled={!value}
        title="Copy"
        className="rounded-lg p-1.5 text-white/50 transition-colors hover:bg-white/[0.06] hover:text-white/90 disabled:cursor-not-allowed disabled:opacity-30"
      >
        {copied ? <Check className="h-4 w-4 text-emerald-300" /> : <Copy className="h-4 w-4" />}
      </button>
      <button
        type="button"
        onClick={() => setShown((s) => !s)}
        disabled={!value}
        title={shown ? 'Hide' : 'Show'}
        className="rounded-lg p-1.5 text-white/50 transition-colors hover:bg-white/[0.06] hover:text-white/90 disabled:cursor-not-allowed disabled:opacity-30"
      >
        {shown ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
      </button>
    </div>
  );
}
