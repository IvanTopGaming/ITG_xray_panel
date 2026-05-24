import api from './api';
import type {
  Tariff,
  TariffWritePayload,
  TariffStatsMap,
  BotTextRow,
  BotTextKeyMeta,
  BotUser,
  BotUserDetail,
  UserTariffGrant,
  GrantBilling,
  GrantRow,
  PaymentListResponse,
  BotSettings,
  BotSettingsUpdate,
} from './types';

export async function listTariffs(): Promise<Tariff[]> {
  const { data } = await api.get<{ tariffs: Tariff[] }>('/bot/tariffs');
  return data.tariffs;
}

export async function createTariff(payload: TariffWritePayload): Promise<Tariff> {
  const { data } = await api.post<Tariff>('/bot/tariffs', payload);
  return data;
}

export async function updateTariff(id: number, payload: TariffWritePayload): Promise<Tariff> {
  const { data } = await api.put<Tariff>(`/bot/tariffs/${id}`, payload);
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

export async function blockBotUser(
  tgId: number
): Promise<{ ok: boolean; cancelled_grants: number; disabled_clients: number }> {
  const { data } = await api.post(`/bot/users/${tgId}/block`);
  return data;
}

export async function unblockBotUser(tgId: number): Promise<{ ok: boolean }> {
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

export async function createGrant(
  tgId: number,
  payload: { tariff_id: number; billing: GrantBilling; note?: string }
): Promise<UserTariffGrant> {
  const { data } = await api.post<UserTariffGrant>(`/bot/users/${tgId}/grants`, payload);
  return data;
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
}

export async function listPayments(filters: PaymentListFilters = {}): Promise<PaymentListResponse> {
  const params = new URLSearchParams();
  if (filters.status) params.set('status', filters.status);
  if (filters.telegram_id) params.set('telegram_id', String(filters.telegram_id));
  if (filters.from) params.set('from', filters.from);
  if (filters.to) params.set('to', filters.to);
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
