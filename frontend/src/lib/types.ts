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
