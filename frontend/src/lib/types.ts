export interface User {
  username: string;
  token: string;
}

export interface SystemStats {
  cpu: number;
  mem_used: number;
  mem_total: number;
  mem_percent: number;
}

export interface Client {
  id: string;
  email: string;
  flow?: string;
  limit_bytes: number;
  expiry_time: number;
  up: number;
  down: number;
  enable: boolean;
  reset_day: number;
  last_seen?: number;
  source_ips?: string[];
  inbound_tag: string;
  preferred_outbound?: string;
  device_limit?: number | null; // null = inherit from inbound
  device_count?: number; // present on list endpoints (batch-injected)
  tariff_id?: number | null; // set when client was provisioned via a tariff
  panel_id?: number | null; // set on the bot user drawer when client lives on a linked panel
  panel_name?: string;
}

export interface ClientDevice {
  id: number;
  device_os: string;
  os_ver: string;
  model: string;
  first_seen: number;
  last_seen: number;
  // Admin-only — present on /api/clients/<id>/devices, absent on /api/sub/<id>/devices
  hwid?: string;
  user_agent?: string;
  request_ip?: string;
  hits?: number;
}

export interface StreamSettings {
  network: string;
  security: string;
  realitySettings?: {
    dest: string;
    serverNames: string[];
    privateKey: string;
    shortIds: string[];
    publicKey?: string;
    fingerprint?: string;
    spiderX?: string;
  };
  tlsSettings?: {
    serverName?: string;
    alpn?: string[];
    certificates?: Array<{
      certificateFile: string;
      keyFile: string;
    }>;
    _utlsFingerprint?: string;
  };
  wsSettings?: { path: string; headers?: { Host?: string } };
  xhttpSettings?: { path?: string; host?: string };
  httpUpgradeSettings?: { path: string; host?: string };
  splitHttpSettings?: { path: string; host?: string };
  grpcSettings?: { serviceName: string };
  ssMethod?: string;
  ssPassword?: string;
  ssNetwork?: string;
  wgSecretKey?: string;
  wgPublicKey?: string;
  wgMTU?: number;
  authUser?: string;
  authPass?: string;
}

export interface Inbound {
  tag: string;
  label?: string | null;
  port: number;
  protocol: string;
  streamSettings: StreamSettings;
  settings: {
    clients: Client[];
  };
  up: number;
  down: number;
  routing_profile_id?: number;
  fallback_address?: string;
  device_limit?: number; // 0 = unlimited (feature off)
  panel_id?: number | null;
  panel_name?: string;
}

export interface Outbound {
  tag: string;
  protocol: string;
  enable?: boolean;
  settings: any;
  streamSettings: any;
  mux: any;
}

export interface OutboundHealth {
  tag: string;
  status: 'up' | 'down' | 'unknown';
  rttMs: number | null;
  checkedAt: number;
  endpoint: string;
  error?: string;
}

export interface Balancer {
  tag: string;
  enable?: boolean;
  selector: string[];
  strategy: string;
  fallback_tag?: string | null;
}

export interface LinkedPanel {
  id: number;
  name: string;
  url: string;
  federation_token: string;
  status: 'online' | 'offline' | 'unknown';
  last_poll: number | null;
  last_error: string | null;
  enable: boolean;
  created_at: number;
}

export interface FederationConfig {
  master_url: string | null;
  master_name: string | null;
  linked_at: number | null;
  link_token: string | null;
  is_linked: boolean;
}

export interface RoutingRule {
  type: string;
  enabled?: boolean;
  domain?: string[];
  ip?: string[];
  port?: string;
  network?: string;
  outboundTag?: string;
  protocol?: string[];
  source?: string[];
  inboundTag?: string[];
  balancerTag?: string;
  user?: string[];
  comment?: string;
}

export interface RoutingProfile {
  id: number;
  name: string;
  enable?: boolean;
  rules: RoutingRule[];
}

// Bot billing — phase 1 (tariffs CRUD)
export type TariffVisibility = 'public' | 'private' | 'archived';

export interface TariffItem {
  id?: number; // optional for new (unsaved) items
  inbound_tag: string;
  label: string;
  traffic_gb: number; // 0 = unlimited
  sort_order: number;
  panel_id?: number | null;
}

export interface Tariff {
  id: number;
  name: string;
  price_rub: number;
  period_days: number;
  visibility: TariffVisibility;
  is_trial: boolean;
  enabled: boolean;
  sort_order: number;
  created_at: string | null;
  updated_at: string | null;
  items: TariffItem[];
}

// Payload type for create/update — id is server-assigned, timestamps server-computed
export type TariffWritePayload = Omit<Tariff, 'id' | 'created_at' | 'updated_at' | 'items'> & {
  items: Omit<TariffItem, 'id'>[];
};

export interface TariffStats {
  active_subs: number;
  revenue_30d: number;
  last_sale_at: string | null;
}

export type TariffStatsMap = Record<number, TariffStats>;

// Bot billing — phase 2 (i18n)
export interface BotTextRow {
  key: string;
  lang: 'ru' | 'en';
  text: string;
  updated_at: string | null;
}

export interface BotTextKeyMeta {
  key: string;
  description: string;
  variables: string[];
  default_ru: string;
  default_en: string;
}

// Bot billing — phase 3 (users + grants)
export type GrantBilling = 'paid' | 'gift' | 'free';

export interface BotUser {
  telegram_id: number;
  username: string | null;
  language: string;
  blocked: boolean;
  first_seen_at: string | null;
  last_seen_at: string | null;
  trial_used_at: string | null;
  clients_count: number;
  grants_count: number;
}

export interface UserTariffGrant {
  id: number;
  telegram_id: number;
  tariff_id: number;
  billing: GrantBilling;
  next_renewal_at: string | null;
  note: string | null;
}

export interface BotUserPayment {
  id: number;
  yookassa_id: string;
  amount_rub: number;
  status: 'pending' | 'succeeded' | 'cancelled' | 'failed';
  tariff_id: number;
  created_at: string | null;
  paid_at: string | null;
}

export interface BotUserDetail extends BotUser {
  clients: Client[];
  grants: UserTariffGrant[];
  payments: BotUserPayment[];
}

export interface GrantRow {
  id: number;
  telegram_id: number;
  username: string | null;
  tariff_id: number;
  tariff_name: string;
  billing: GrantBilling;
  next_renewal_at: string | null;
  note: string | null;
}

// Bot billing — phase 4 (payments + settings)
export type PaymentStatus = 'pending' | 'succeeded' | 'cancelled' | 'failed';

export interface Payment {
  id: number;
  yookassa_id: string;
  telegram_id: number;
  tariff_id: number;
  tariff_name: string;
  amount_rub: number;
  status: PaymentStatus;
  confirmation_url: string | null;
  created_at: string;
  paid_at: string | null;
}

export interface PaymentListResponse {
  items: Payment[];
  total: number;
  stats: {
    month_count: number;
    month_amount_rub: number;
  };
}

export interface BotSettings {
  yookassa_shop_id: string;
  yookassa_return_url: string;
  yookassa_secret_key: string;
  bot_token: string;
  bot_service_token: string;
  has_yookassa_secret: boolean;
  has_bot_service_token: boolean;
  has_bot_token: boolean;
  admin_ids: number[];
  telegram_proxy_url: string;
  display_timezone: string;
  bot_config_version: number;
}

export interface BotSettingsUpdate {
  yookassa_shop_id?: string;
  yookassa_secret_key?: string;
  yookassa_return_url?: string;
  bot_token?: string;
  admin_ids?: number[];
  telegram_proxy_url?: string;
  display_timezone?: string;
}
