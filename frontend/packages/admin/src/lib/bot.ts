import api from '@ui/lib/api';
import { epochMsFromLocalDateTimeInput } from '@ui/lib/datetime';
import type {
  Tariff,
  TariffWritePayload,
  TariffStatsMap,
  BotTextRow,
  BotTextKeyMeta,
  BotUser,
  BotUserDetail,
  UserWarningHistory,
  UserTariffGrant,
  GrantBilling,
  GrantRow,
  PaymentListResponse,
  BotSettings,
  BotSettingsUpdate,
  BackfillSummary,
} from '@ui/lib/types';

export async function listTariffs(): Promise<Tariff[]> {
  const { data } = await api.get<{ tariffs: Tariff[] }>('/bot/tariffs');
  return data.tariffs;
}

export async function createTariff(payload: TariffWritePayload): Promise<Tariff> {
  const { data } = await api.post<Tariff>('/bot/tariffs', payload);
  return data;
}

export async function updateTariff(
  id: number,
  payload: TariffWritePayload
): Promise<Tariff & { backfill?: BackfillSummary | null }> {
  const { data } = await api.put<Tariff & { backfill?: BackfillSummary | null }>(
    `/bot/tariffs/${id}`,
    payload
  );
  return data;
}

export async function archiveTariff(id: number): Promise<Tariff> {
  const { data } = await api.delete<Tariff>(`/bot/tariffs/${id}`);
  return data;
}

export async function deleteTariffPermanent(id: number): Promise<void> {
  await api.delete(`/bot/tariffs/${id}/permanent`);
}

export async function restoreTariff(id: number): Promise<Tariff> {
  const { data } = await api.post<Tariff>(`/bot/tariffs/${id}/restore`);
  return data;
}

export async function getTariffStats(): Promise<TariffStatsMap> {
  const { data } = await api.get<{ stats: TariffStatsMap }>('/bot/tariffs/stats');
  return data.stats;
}

export interface PanelFailure {
  panel_id: number;
  panel_name?: string | null;
  error: string;
}

export async function blockBotUser(tgId: number): Promise<{
  ok: boolean;
  cancelled_grants: number;
  disabled_clients: number;
  remote_disabled: number;
  panel_failures: PanelFailure[];
}> {
  const { data } = await api.post(`/bot/users/${tgId}/block`);
  return data;
}

export async function resetSubToken(tgId: number): Promise<{ sub_url: string | null }> {
  const { data } = await api.post(`/bot/users/${tgId}/reset-sub-token`);
  return data;
}

export async function unblockBotUser(tgId: number): Promise<{
  ok: boolean;
  re_enabled: number;
  remote_re_enabled: number;
  panel_failures: PanelFailure[];
}> {
  const { data } = await api.post(`/bot/users/${tgId}/unblock`);
  return data;
}

export async function revokeTariff(
  tgId: number,
  tariffId: number
): Promise<{
  ok: boolean;
  telegram_id: number;
  tariff_id: number;
  disabled_clients: number;
  revoked_grants: number;
  remote_disabled?: number;
  panel_failures?: PanelFailure[];
}> {
  const { data } = await api.delete(`/bot/users/${tgId}/tariffs/${tariffId}`);
  return data;
}

export async function duplicateTariff(id: number): Promise<Tariff> {
  const { data } = await api.post<Tariff>(`/bot/tariffs/${id}/duplicate`);
  return data;
}

export async function listBotTexts(): Promise<BotTextRow[]> {
  const { data } = await api.get<{ texts: BotTextRow[] }>('/bot/texts');
  return data.texts;
}

export async function listBotTextKeys(): Promise<BotTextKeyMeta[]> {
  const { data } = await api.get<{ keys: BotTextKeyMeta[] }>('/bot/texts/keys');
  return data.keys;
}

export async function updateBotText(
  key: string,
  lang: 'ru' | 'en',
  text: string
): Promise<BotTextRow> {
  const { data } = await api.put<BotTextRow>(`/bot/texts/${encodeURIComponent(key)}`, {
    lang,
    text,
  });
  return data;
}

export async function resetBotText(key: string, lang: 'ru' | 'en'): Promise<void> {
  await api.delete(`/bot/texts/${encodeURIComponent(key)}?lang=${lang}`);
}

export async function listBotUsers(): Promise<BotUser[]> {
  const { data } = await api.get<{ users: BotUser[] }>('/bot/users');
  return data.users;
}

export async function getBotUser(tgId: number): Promise<BotUserDetail> {
  const { data } = await api.get<BotUserDetail>(`/bot/users/${tgId}`);
  return data;
}

export async function getUserWarnings(tgId: number, offset = 0): Promise<UserWarningHistory> {
  const { data } = await api.get<UserWarningHistory>(`/bot/users/${tgId}/warnings`, {
    params: { limit: 20, offset },
  });
  if (!data || !Array.isArray(data.items) || typeof data.sent !== 'number') {
    throw new Error('Invalid warning history response');
  }
  return data;
}

export interface GrantWriteResult extends UserTariffGrant {
  pending: boolean;
  panel_failures?: PanelFailure[];
  operation_id?: string;
}

export async function createGrant(
  tgId: number,
  payload: {
    tariff_id: number;
    billing: GrantBilling;
    access_until?: string | null;
    note?: string;
    silent?: boolean;
  }
): Promise<GrantWriteResult> {
  const { data, status } = await api.post<GrantWriteResult>(`/bot/users/${tgId}/grants`, payload);
  return { ...data, pending: status === 202 };
}

export async function updateGrantTerm(
  tgId: number,
  tariffId: number,
  payload: { access_until: string | null }
): Promise<GrantWriteResult> {
  const { data, status } = await api.patch<GrantWriteResult>(
    `/bot/users/${tgId}/grants/${tariffId}`,
    payload
  );
  return { ...data, pending: status === 202 };
}

export async function listGrants(): Promise<GrantRow[]> {
  const { data } = await api.get<{ rows: GrantRow[] }>('/bot/grants');
  return data.rows;
}

export interface PaymentListFilters {
  status?: string;
  telegram_id?: number;
  from?: string;
  to?: string;
  limit?: number;
  offset?: number;
}

export async function listPayments(filters: PaymentListFilters = {}): Promise<PaymentListResponse> {
  const params = new URLSearchParams();
  if (filters.status) params.set('status', filters.status);
  if (filters.telegram_id) params.set('telegram_id', String(filters.telegram_id));
  if (filters.from) {
    params.set(
      'from',
      new Date(epochMsFromLocalDateTimeInput(`${filters.from}T00:00`)).toISOString()
    );
  }
  if (filters.to) {
    const nextDay = new Date(`${filters.to}T00:00:00Z`);
    nextDay.setUTCDate(nextDay.getUTCDate() + 1);
    const end = epochMsFromLocalDateTimeInput(`${nextDay.toISOString().slice(0, 10)}T00:00`);
    params.set('to_exclusive', new Date(end).toISOString());
  }
  if (filters.limit !== undefined) params.set('limit', String(filters.limit));
  if (filters.offset !== undefined) params.set('offset', String(filters.offset));
  const qs = params.toString();
  const r = await api.get<PaymentListResponse>(`/bot/payments${qs ? `?${qs}` : ''}`);
  return r.data;
}

export async function getBotSettings(): Promise<BotSettings> {
  const r = await api.get<BotSettings>('/bot/settings');
  return r.data;
}

export async function updateBotSettings(payload: BotSettingsUpdate): Promise<void> {
  await api.put('/bot/settings', payload);
}

export async function rotateBotServiceToken(): Promise<string> {
  const r = await api.post<{ token: string }>('/bot/settings/rotate-bot-service-token');
  return r.data.token;
}
