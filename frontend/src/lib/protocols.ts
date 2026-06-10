import { Inbound, Client } from './types';
import { panelBase } from './panelBase';

function normalizeRealityPublicKey(value: string): string {
  const key = (value || '').trim();
  if (!key) return '';

  const standard = key.replace(/-/g, '+').replace(/_/g, '/');
  const padded = standard + '='.repeat((4 - (standard.length % 4)) % 4);

  try {
    const raw = atob(padded);
    if (raw.length === 32) {
      return btoa(raw).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/g, '');
    }
  } catch {}

  return key.replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/g, '');
}

function isShadowsocks2022Method(method: string): boolean {
  const value = (method || '').trim().toLowerCase();
  return [
    '2022-blake3-aes-128-gcm',
    '2022-blake3-aes-256-gcm',
    '2022-blake3-chacha20-poly1305',
  ].includes(value);
}

function normalizeShadowsocks2022Key(value: string): string {
  const key = (value || '').trim();
  if (!key) return '';

  const standard = key.replace(/-/g, '+').replace(/_/g, '/');
  const padded = standard + '='.repeat((4 - (standard.length % 4)) % 4);
  try {
    const raw = atob(padded);
    return btoa(raw);
  } catch {
    return key;
  }
}

function resolvePanelApiPath(path: string): string {
  const panelBaseNorm = panelBase.endsWith('/') ? panelBase : `${panelBase}/`;
  const normalizedPath = path.startsWith('/') ? path.slice(1) : path;
  return new URL(`${panelBaseNorm}${normalizedPath}`, window.location.origin).toString();
}

function appendPathHostParams(
  params: URLSearchParams,
  type: string,
  path: string,
  host: string
): void {
  if (['ws', 'xhttp', 'httpupgrade', 'splithttp'].includes(type)) {
    params.set('path', path);
    if (host) params.set('host', host);
  }
}

export function generateLink(
  inbound: Inbound,
  client: Client,
  host: string = window.location.hostname
): string {
  const { protocol, port, streamSettings } = inbound;
  const uuid = client.id;
  const name = encodeURIComponent(client.email);
  const type = streamSettings.network || 'tcp';
  const security = streamSettings.security || 'none';
  const path =
    streamSettings.wsSettings?.path ||
    streamSettings.xhttpSettings?.path ||
    streamSettings.httpUpgradeSettings?.path ||
    streamSettings.splitHttpSettings?.path ||
    '/';
  const wsHost =
    streamSettings.wsSettings?.headers?.Host ||
    streamSettings.xhttpSettings?.host ||
    streamSettings.httpUpgradeSettings?.host ||
    streamSettings.splitHttpSettings?.host ||
    '';
  const serviceName = streamSettings.grpcSettings?.serviceName || '';
  const tlsSni = streamSettings.tlsSettings?.serverName || wsHost;
  const tlsAlpn = (streamSettings.tlsSettings?.alpn || []).filter(Boolean);
  const tlsFp = (streamSettings.tlsSettings?._utlsFingerprint || '').trim();

  if (protocol === 'vless' || protocol === 'vmess') {
    if (protocol === 'vmess') {
      const vmessConfig = {
        v: '2',
        ps: client.email,
        add: host,
        port: port,
        id: uuid,
        aid: '0',
        net: type,
        type: 'none',
        host: wsHost,
        path: path,
        tls: security,
      };
      if (security === 'tls' && tlsSni) (vmessConfig as any).sni = tlsSni;
      return `vmess://${btoa(JSON.stringify(vmessConfig))}`;
    }

    const params = new URLSearchParams({
      type,
      security,
    });
    appendPathHostParams(params, type, path, wsHost);
    if (type === 'grpc') params.set('serviceName', serviceName);

    if (security === 'reality') {
      const r = streamSettings.realitySettings;
      if (r) {
        const pbk = normalizeRealityPublicKey(r.publicKey || '');
        params.set('pbk', pbk);
        params.set('fp', r.fingerprint || 'chrome');
        params.set('sni', r.serverNames?.[0] || 'google.com');
        params.set('sid', r.shortIds?.[0] || '');
        if (r.spiderX) params.set('spx', r.spiderX);
      }
    }

    if (security === 'tls') {
      if (tlsSni) params.set('sni', tlsSni);
      if (tlsAlpn.length > 0) params.set('alpn', tlsAlpn.join(','));
      if (tlsFp) params.set('fp', tlsFp);
    }

    if (client.flow) params.set('flow', client.flow);
    return `${protocol}://${encodeURIComponent(uuid)}@${host}:${port}?${params.toString()}#${name}`;
  }

  if (protocol === 'trojan') {
    const params = new URLSearchParams({
      security,
      type,
    });
    appendPathHostParams(params, type, path, wsHost);
    if (type === 'grpc') params.set('serviceName', serviceName);
    if (security === 'reality') {
      const r = streamSettings.realitySettings;
      if (r) {
        const pbk = normalizeRealityPublicKey(r.publicKey || '');
        params.set('pbk', pbk);
        params.set('fp', r.fingerprint || 'chrome');
        params.set('sni', r.serverNames?.[0] || 'google.com');
        params.set('sid', r.shortIds?.[0] || '');
        if (r.spiderX) params.set('spx', r.spiderX);
      }
    }
    if (security === 'tls') {
      if (tlsSni) params.set('sni', tlsSni);
      if (tlsAlpn.length > 0) params.set('alpn', tlsAlpn.join(','));
      if (tlsFp) params.set('fp', tlsFp);
    }
    return `trojan://${encodeURIComponent(uuid)}@${host}:${port}?${params.toString()}#${name}`;
  }

  if (protocol === 'shadowsocks') {
    const method = streamSettings.ssMethod || 'chacha20-poly1305';
    let serverPassword = streamSettings.ssPassword || '';
    let userPassword = client.id;
    if (isShadowsocks2022Method(method)) {
      serverPassword = normalizeShadowsocks2022Key(serverPassword);
      userPassword = normalizeShadowsocks2022Key(userPassword);
    }
    let userPart = `${method}:${userPassword}`;
    if (isShadowsocks2022Method(method)) {
      userPart = `${method}:${serverPassword}:${userPassword}`;
    }
    return `ss://${btoa(userPart)}@${host}:${port}#${name}`;
  }

  if (protocol === 'wireguard') {
    const mtu = Number(streamSettings.wgMTU || 0);
    const mtuLine = mtu > 0 ? `\nMTU = ${mtu}` : '';
    return `[Interface]\nPrivateKey = ${uuid}\nAddress = 172.19.0.x/32\nDNS = 1.1.1.1${mtuLine}\n\n[Peer]\nPublicKey = ${streamSettings.wgPublicKey || 'SERVER_PUB_KEY'}\nEndpoint = ${host}:${port}\nAllowedIPs = 0.0.0.0/0\nPersistentKeepalive = 25`;
  }

  return '';
}

export function generateSubscriptionUrl(client: Client): string {
  return resolvePanelApiPath(`api/sub/${encodeURIComponent(client.id)}`);
}
