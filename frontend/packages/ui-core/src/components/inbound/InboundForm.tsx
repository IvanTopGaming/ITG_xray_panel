import { useEffect, useMemo, useState } from 'react';
import { useForm, useWatch } from 'react-hook-form';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Button } from '@ui/components/ui/Button';
import { Input } from '@ui/components/ui/Input';
import { Select } from '@ui/components/ui/Select';
import { Inbound, RoutingProfile } from '@ui/lib/types';
import api from '@ui/lib/api';
import { toast } from 'react-toastify';
import { Copy, Eye, EyeOff, RefreshCw } from 'lucide-react';
import { hasLocalXray } from '@ui/lib/panelRole';
import { useLinkedPanels } from '@ui/hooks/useLinkedPanels';

interface InboundFormProps {
  inbound?: Inbound;
  onSuccess: () => void;
  onCancel: () => void;
}

const TRANSPORT_PROTOCOLS = ['vless', 'vmess', 'trojan', 'shadowsocks'];
const TLS_CAPABLE_PROTOCOLS = ['vless', 'vmess', 'trojan', 'shadowsocks'];
const XHTTP_PROTOCOLS = ['vless', 'trojan'];
const SECURITY_OPTIONS_BY_PROTOCOL: Record<string, { value: string; label: string }[]> = {
  vless: [
    { value: 'none', label: 'None' },
    { value: 'tls', label: 'TLS' },
    { value: 'reality', label: 'Reality' },
  ],
  vmess: [
    { value: 'none', label: 'None' },
    { value: 'tls', label: 'TLS' },
  ],
  trojan: [
    { value: 'none', label: 'None' },
    { value: 'tls', label: 'TLS' },
    { value: 'reality', label: 'Reality' },
  ],
  shadowsocks: [
    { value: 'none', label: 'None' },
    { value: 'tls', label: 'TLS' },
  ],
};

const getTransportOptions = (protocol: string, security: string) => {
  if (protocol === 'shadowsocks') {
    return [
      { value: 'tcp', label: 'TCP' },
      { value: 'udp', label: 'UDP' },
      { value: 'tcp,udp', label: 'TCP+UDP' },
    ];
  }

  const options: { value: string; label: string }[] = [
    { value: 'tcp', label: 'TCP' },
    { value: 'grpc', label: 'gRPC' },
  ];

  if (XHTTP_PROTOCOLS.includes(protocol)) {
    options.push({ value: 'xhttp', label: 'XHTTP' });
  }

  if (security !== 'reality') {
    options.push(
      { value: 'ws', label: 'WebSocket' },
      { value: 'httpupgrade', label: 'HTTPUpgrade' },
      { value: 'splithttp', label: 'SplitHTTP' }
    );
  }

  return options;
};

export function InboundForm({ inbound, onSuccess, onCancel }: InboundFormProps) {
  const isEdit = !!inbound;
  const queryClient = useQueryClient();
  const [showAuthPass, setShowAuthPass] = useState(false);
  const [targetPanelId, setTargetPanelId] = useState<number | null>(null);

  const { data: panels } = useLinkedPanels(!isEdit);

  useEffect(() => {
    if (isEdit || hasLocalXray || targetPanelId != null) return;
    const firstPanel = panels?.[0];
    if (firstPanel) setTargetPanelId(firstPanel.id);
  }, [isEdit, panels, targetPanelId]);

  const getDefaults = () => {
    if (inbound) {
      const ss = inbound.streamSettings;
      const storedNetwork =
        inbound.protocol === 'shadowsocks'
          ? ss?.ssNetwork || ss?.network || 'tcp'
          : ss?.network || 'tcp';
      return {
        tag: inbound.tag,
        label: inbound.label || '',
        port: inbound.port,
        protocol: inbound.protocol,
        network: storedNetwork,
        security: ss?.security || 'none',
        routing_profile_id: inbound.routing_profile_id || '',
        fallback_address: inbound.fallback_address || '',

        realityDest: ss?.realitySettings?.dest || 'www.google.com:443',
        realitySNI: ss?.realitySettings?.serverNames?.[0] || 'www.google.com',
        realityPrivateKey: ss?.realitySettings?.privateKey || '',
        realityPublicKey: ss?.realitySettings?.publicKey || '',
        realityShortIds: ss?.realitySettings?.shortIds?.join(', ') || '',
        realityFingerprint: ss?.realitySettings?.fingerprint || 'chrome',
        realitySpiderX: ss?.realitySettings?.spiderX || '',
        tlsServerName: ss?.tlsSettings?.serverName || '',
        tlsAlpn: ss?.tlsSettings?.alpn?.join(', ') || '',
        tlsCertFile: ss?.tlsSettings?.certificates?.[0]?.certificateFile || '',
        tlsKeyFile: ss?.tlsSettings?.certificates?.[0]?.keyFile || '',
        tlsUTLSFingerprint: ss?.tlsSettings?._utlsFingerprint || '',

        wsPath:
          ss?.wsSettings?.path ||
          ss?.xhttpSettings?.path ||
          ss?.httpUpgradeSettings?.path ||
          ss?.splitHttpSettings?.path ||
          '/',
        wsHost:
          ss?.wsSettings?.headers?.Host ||
          ss?.xhttpSettings?.host ||
          ss?.httpUpgradeSettings?.host ||
          ss?.splitHttpSettings?.host ||
          '',

        grpcServiceName: ss?.grpcSettings?.serviceName || 'grpc',

        ssMethod: ss?.ssMethod || '2022-blake3-aes-128-gcm',
        ssPassword: ss?.ssPassword || '',
        wgSecretKey: ss?.wgSecretKey || '',
        wgPublicKey: ss?.wgPublicKey || '',
        wgMTU: ss?.wgMTU || '',
        authUser: ss?.authUser || '',
        authPass: ss?.authPass || '',
      };
    }
    return {
      tag: '',
      label: '',
      port: 443,
      protocol: 'vless',
      network: 'tcp',
      security: 'none',
      routing_profile_id: '',
      fallback_address: '',
      realityDest: 'www.google.com:443',
      realitySNI: 'www.google.com',
      realityPrivateKey: '',
      realityPublicKey: '',
      realityShortIds: '',
      realityFingerprint: 'chrome',
      realitySpiderX: '',
      tlsServerName: '',
      tlsAlpn: '',
      tlsCertFile: '',
      tlsKeyFile: '',
      tlsUTLSFingerprint: '',
      wsPath: '/',
      wsHost: '',
      grpcServiceName: 'grpc',
      ssMethod: '2022-blake3-aes-128-gcm',
      ssPassword: '',
      wgSecretKey: '',
      wgPublicKey: '',
      wgMTU: '',
      authUser: '',
      authPass: '',
    };
  };

  const { register, handleSubmit, control, setValue, getValues } = useForm({
    defaultValues: getDefaults(),
  });

  const protocol = useWatch({ control, name: 'protocol' });
  const network = useWatch({ control, name: 'network' });
  const security = useWatch({ control, name: 'security' });
  const routingProfileId = useWatch({ control, name: 'routing_profile_id' });
  const realityFingerprint = useWatch({ control, name: 'realityFingerprint' });
  const tlsUTLSFingerprint = useWatch({ control, name: 'tlsUTLSFingerprint' });
  const ssMethod = useWatch({ control, name: 'ssMethod' });

  const supportsTransport = TRANSPORT_PROTOCOLS.includes(protocol);
  const securityOptions = SECURITY_OPTIONS_BY_PROTOCOL[protocol] || [];
  const transportOptions = useMemo(
    () => getTransportOptions(protocol, security),
    [protocol, security]
  );
  const allowedNetworks = useMemo(
    () => transportOptions.map((opt) => opt.value),
    [transportOptions]
  );

  useEffect(() => {
    if (['socks', 'http'].includes(protocol)) {
      if (network !== 'tcp') setValue('network', 'tcp');
      if (security !== 'none') setValue('security', 'none');
    }
    if (supportsTransport && !allowedNetworks.includes(network)) {
      setValue('network', 'tcp');
    }
    if (securityOptions.length > 0 && !securityOptions.some((opt) => opt.value === security)) {
      setValue('security', 'none');
    }
    if (security === 'reality') {
      if (['ws', 'httpupgrade', 'splithttp'].includes(network)) {
        setValue('network', 'tcp');
      }
    }
  }, [protocol, security, network, setValue, supportsTransport, securityOptions, allowedNetworks]);

  const { data: profiles } = useQuery({
    queryKey: ['routing-profiles'],
    queryFn: async () => (await api.get<RoutingProfile[]>('/routing-profiles')).data,
  });

  const appendShortId = (rawShortId: string) => {
    const shortId = String(rawShortId || '').trim();
    if (!shortId) return;

    const existing = String(getValues('realityShortIds') || '')
      .split(',')
      .map((item) => item.trim())
      .filter(Boolean);
    const merged = Array.from(new Set([...existing, shortId]));
    setValue('realityShortIds', merged.join(', '));
  };

  const copyAuthPassword = async () => {
    const password = String(getValues('authPass') || '');
    if (!password) {
      toast.error('Password is empty');
      return;
    }
    try {
      await navigator.clipboard.writeText(password);
      toast.success('Password copied');
    } catch {
      toast.error('Failed to copy password');
    }
  };

  const mutation = useMutation({
    mutationFn: (data: any) => {
      if (isEdit) {
        const qs = inbound!.panel_id != null ? `?panel_id=${inbound!.panel_id}` : '';
        return api.put(`/inbounds/${inbound!.tag}${qs}`, data);
      }
      const url = targetPanelId != null ? `/inbounds?panel_id=${targetPanelId}` : '/inbounds';
      return api.post(url, data);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['inbounds'] });
      toast.success(isEdit ? 'Inbound updated' : 'Inbound created');
      onSuccess();
    },
    onError: (err: any) => toast.error(err.response?.data?.error || 'Save failed'),
  });

  const generateKeysMutation = useMutation({
    mutationFn: () => api.post('/server-keys', { type: 'reality' }),
    onSuccess: (res) => {
      setValue('realityPrivateKey', res.data.privateKey);
      setValue('realityPublicKey', res.data.publicKey);
      if (res.data.shortId) appendShortId(res.data.shortId);
      toast.success('Keys generated');
    },
  });

  const generateShortIdMutation = useMutation({
    mutationFn: () => api.post('/server-keys', { type: 'short-id' }),
    onSuccess: (res) => {
      appendShortId(res.data.shortId);
      toast.success('Short ID generated');
    },
  });

  const generateWgKeysMutation = useMutation({
    mutationFn: () => api.post('/server-keys', { type: 'wireguard' }),
    onSuccess: (res) => {
      setValue('wgSecretKey', res.data.privateKey);
      setValue('wgPublicKey', res.data.publicKey);
      toast.success('WireGuard keys generated');
    },
  });

  const generateProxyAuthMutation = useMutation({
    mutationFn: () => api.post('/server-keys', { type: 'proxy-auth' }),
    onSuccess: (res) => {
      setValue('authUser', res.data.username || '');
      setValue('authPass', res.data.password || '');
      toast.success('Username/password generated');
    },
  });

  const generateShadowsocksPasswordMutation = useMutation({
    mutationFn: () =>
      api.post('/server-keys', {
        type: 'ss-password',
        method: String(getValues('ssMethod') || ''),
      }),
    onSuccess: (res) => {
      setValue('ssPassword', res.data.password || '');
      toast.success('Shadowsocks password generated');
    },
  });

  const onSubmit = (data: any) => {
    const payload: any = {
      tag: data.tag,
      label: data.label || null,
      port: Number(data.port),
      protocol: data.protocol,
      routing_profile_id: data.routing_profile_id ? Number(data.routing_profile_id) : null,
      fallback_address: data.fallback_address,
      network: data.network,
      security: data.security,
    };

    if (data.protocol === 'shadowsocks') {
      payload.ssNetwork = data.network;
      payload.network = 'tcp';
    }

    if (['ws', 'xhttp', 'httpupgrade', 'splithttp'].includes(data.network)) {
      payload.wsPath = data.wsPath;
      payload.wsHost = data.wsHost;
    } else if (data.network === 'grpc') {
      payload.grpcServiceName = data.grpcServiceName;
    }

    if (data.security === 'reality') {
      payload.realityDest = data.realityDest;
      payload.realitySNI = data.realitySNI;
      payload.realityPrivateKey = data.realityPrivateKey;
      payload.realityPublicKey = data.realityPublicKey;
      payload.realityShortIds = data.realityShortIds;
      payload.realityFingerprint = data.realityFingerprint;
      payload.realitySpiderX = data.realitySpiderX;
    }
    if (data.security === 'tls' && TLS_CAPABLE_PROTOCOLS.includes(data.protocol)) {
      payload.tlsServerName = data.tlsServerName;
      payload.tlsAlpn = data.tlsAlpn;
      payload.tlsCertFile = data.tlsCertFile;
      payload.tlsKeyFile = data.tlsKeyFile;
      payload.tlsUTLSFingerprint = data.tlsUTLSFingerprint;
    }

    if (data.protocol === 'shadowsocks') {
      payload.ssMethod = data.ssMethod;
      payload.ssPassword = data.ssPassword;
    }

    if (data.protocol === 'wireguard') {
      payload.wgSecretKey = data.wgSecretKey;
      payload.wgPublicKey = data.wgPublicKey;
      payload.wgMTU = data.wgMTU;
    }
    if (['socks', 'http'].includes(data.protocol)) {
      payload.authUser = data.authUser;
      payload.authPass = data.authPass;
    }

    mutation.mutate(payload);
  };

  const isTransportAvailable = supportsTransport;
  const isSecurityAvailable = securityOptions.length > 0;

  return (
    <form onSubmit={handleSubmit(onSubmit)} className="space-y-5" autoComplete="off">
      <input
        type="text"
        name="panel_fake_username"
        autoComplete="username"
        className="hidden"
        tabIndex={-1}
      />
      <input
        type="password"
        name="panel_fake_password"
        autoComplete="current-password"
        className="hidden"
        tabIndex={-1}
      />
      {!isEdit && panels && panels.length > 0 && (
        <Select
          label="Target Panel"
          value={targetPanelId != null ? String(targetPanelId) : 'local'}
          onChange={(e) => {
            const val = e.target.value === 'local' ? null : Number(e.target.value);
            setTargetPanelId(val);
          }}
          options={[
            ...(hasLocalXray ? [{ value: 'local', label: 'Master (local)' }] : []),
            ...panels.map((p) => ({ value: String(p.id), label: p.name })),
          ]}
        />
      )}

      <div className="grid grid-cols-1">
        <Input
          label="Display Label"
          placeholder='e.g. "🇩🇪 Germany — VLESS"'
          {...register('label')}
        />
      </div>
      <div className="grid grid-cols-2 gap-4">
        <Input label="Tag" {...register('tag', { required: true })} disabled={isEdit} />
        <Input label="Port" type="number" {...register('port', { required: true })} />
      </div>

      <div className="grid grid-cols-2 gap-4">
        <Select
          label="Protocol"
          {...register('protocol')}
          value={protocol}
          options={[
            { value: 'vless', label: 'VLESS' },
            { value: 'vmess', label: 'VMess' },
            { value: 'trojan', label: 'Trojan' },
            { value: 'shadowsocks', label: 'Shadowsocks' },
            { value: 'wireguard', label: 'WireGuard' },
            { value: 'socks', label: 'Socks' },
            { value: 'http', label: 'HTTP' },
          ]}
        />

        {isTransportAvailable && (
          <Select
            label={protocol === 'shadowsocks' ? 'Network' : 'Transport'}
            {...register('network')}
            value={network}
            options={transportOptions}
          />
        )}
      </div>

      <div className={`grid gap-4 ${isSecurityAvailable ? 'grid-cols-2' : 'grid-cols-1'}`}>
        {isSecurityAvailable && (
          <Select
            label="Security"
            {...register('security')}
            value={security}
            options={securityOptions}
          />
        )}
        <Select
          label="Routing Profile"
          {...register('routing_profile_id')}
          value={String(routingProfileId ?? '')}
          options={[
            { value: '', label: 'None (Direct)' },
            ...(profiles?.map((p) => ({ value: String(p.id), label: p.name })) || []),
          ]}
        />
      </div>

      <div className="grid grid-cols-1">
        <Input
          label="Fallback / Dest"
          {...register('fallback_address')}
          placeholder="Optional fallback address"
        />
      </div>

      {['vless', 'trojan'].includes(protocol) && security === 'reality' && isSecurityAvailable && (
        <div className="bg-white/5 p-4 rounded-xl border border-white/5 space-y-4">
          <h4 className="text-sm font-bold text-primary uppercase">
            {protocol.toUpperCase()} + Reality
          </h4>
          <div className="grid grid-cols-2 gap-4">
            <Input label="SNI" {...register('realitySNI')} />
            <Input label="Dest" {...register('realityDest')} />
          </div>
          <div className="grid grid-cols-[1fr_auto] gap-2 items-end">
            <Input
              label="Private Key"
              {...register('realityPrivateKey')}
              className="font-mono text-xs"
            />
            <Button
              type="button"
              size="icon"
              variant="secondary"
              onClick={() => generateKeysMutation.mutate()}
            >
              <RefreshCw
                size={16}
                className={generateKeysMutation.isPending ? 'animate-spin' : ''}
              />
            </Button>
          </div>
          <Input
            label="Public Key"
            {...register('realityPublicKey')}
            className="font-mono text-xs"
          />
          <div className="grid grid-cols-[1fr_auto] gap-2 items-end">
            <Input label="Short IDs (comma separated)" {...register('realityShortIds')} />
            <Button
              type="button"
              size="icon"
              variant="secondary"
              onClick={() => generateShortIdMutation.mutate()}
            >
              <RefreshCw
                size={16}
                className={generateShortIdMutation.isPending ? 'animate-spin' : ''}
              />
            </Button>
          </div>
          <div className="grid grid-cols-2 gap-4">
            <Select
              label="Fingerprint"
              {...register('realityFingerprint')}
              value={realityFingerprint}
              options={[
                { value: 'chrome', label: 'chrome' },
                { value: 'firefox', label: 'firefox' },
                { value: 'safari', label: 'safari' },
                { value: 'edge', label: 'edge' },
                { value: 'ios', label: 'ios' },
                { value: 'android', label: 'android' },
                { value: 'random', label: 'random' },
              ]}
            />
            <Input label="SpiderX" {...register('realitySpiderX')} placeholder="/" />
          </div>
        </div>
      )}

      {TLS_CAPABLE_PROTOCOLS.includes(protocol) && security === 'tls' && isSecurityAvailable && (
        <div className="bg-white/5 p-4 rounded-xl border border-white/5 space-y-4">
          <h4 className="text-sm font-bold text-secondary uppercase">
            {protocol.toUpperCase()} + TLS
          </h4>
          <div className="grid grid-cols-2 gap-4">
            <Input
              label="SNI / Server Name"
              {...register('tlsServerName')}
              placeholder="example.com"
            />
            <Input
              label="ALPN (comma separated)"
              {...register('tlsAlpn')}
              placeholder="h2,http/1.1"
            />
          </div>
          <div className="grid grid-cols-2 gap-4">
            <Input
              label="Certificate File"
              {...register('tlsCertFile')}
              placeholder="/etc/xray/certs/site.pem"
            />
            <Input
              label="Private Key File"
              {...register('tlsKeyFile')}
              placeholder="/etc/xray/certs/site.key"
            />
          </div>
          <Select
            label="uTLS Fingerprint"
            {...register('tlsUTLSFingerprint')}
            value={tlsUTLSFingerprint}
            options={[
              { value: '', label: 'None' },
              { value: 'chrome', label: 'chrome' },
              { value: 'firefox', label: 'firefox' },
              { value: 'safari', label: 'safari' },
              { value: 'edge', label: 'edge' },
              { value: 'ios', label: 'ios' },
              { value: 'android', label: 'android' },
              { value: 'random', label: 'random' },
            ]}
          />
        </div>
      )}

      {['ws', 'xhttp', 'httpupgrade', 'splithttp'].includes(network) && isTransportAvailable && (
        <div className="bg-white/5 p-4 rounded-xl border border-white/5 space-y-4">
          <h4 className="text-sm font-bold text-secondary uppercase">
            {network === 'ws'
              ? 'WebSocket'
              : network === 'xhttp'
                ? 'XHTTP'
                : network === 'httpupgrade'
                  ? 'HTTPUpgrade'
                  : 'SplitHTTP'}
          </h4>
          <div className="grid grid-cols-2 gap-4">
            <Input label="Path" {...register('wsPath')} />
            <Input label="Host" {...register('wsHost')} />
          </div>
        </div>
      )}

      {network === 'grpc' && isTransportAvailable && (
        <div className="bg-white/5 p-4 rounded-xl border border-white/5 space-y-4">
          <h4 className="text-sm font-bold text-secondary uppercase">gRPC</h4>
          <Input label="Service Name" {...register('grpcServiceName')} />
        </div>
      )}

      {protocol === 'shadowsocks' && (
        <div className="bg-white/5 p-4 rounded-xl border border-white/5 space-y-4">
          <h4 className="text-sm font-bold text-secondary uppercase">Shadowsocks</h4>
          <div className="grid grid-cols-2 gap-4">
            <Select
              label="Method"
              {...register('ssMethod')}
              value={ssMethod}
              options={[
                { value: '2022-blake3-aes-128-gcm', label: '2022-blake3-aes-128-gcm' },
                { value: '2022-blake3-aes-256-gcm', label: '2022-blake3-aes-256-gcm' },
                { value: 'chacha20-poly1305', label: 'chacha20-poly1305' },
                { value: 'aes-128-gcm', label: 'aes-128-gcm' },
                { value: 'aes-256-gcm', label: 'aes-256-gcm' },
              ]}
            />
            <div className="grid grid-cols-[1fr_auto] gap-2 items-end">
              <Input
                label="Server Password"
                {...register('ssPassword', { required: protocol === 'shadowsocks' })}
              />
              <Button
                type="button"
                size="icon"
                variant="secondary"
                onClick={() => generateShadowsocksPasswordMutation.mutate()}
              >
                <RefreshCw
                  size={16}
                  className={generateShadowsocksPasswordMutation.isPending ? 'animate-spin' : ''}
                />
              </Button>
            </div>
          </div>
        </div>
      )}

      {protocol === 'wireguard' && (
        <div className="bg-white/5 p-4 rounded-xl border border-white/5 space-y-4">
          <h4 className="text-sm font-bold text-secondary uppercase">WireGuard</h4>
          <div className="flex gap-2 items-end">
            <Input
              label="Server Private Key"
              {...register('wgSecretKey')}
              className="font-mono text-xs"
            />
            <Button
              type="button"
              variant="secondary"
              onClick={() => generateWgKeysMutation.mutate()}
            >
              <RefreshCw
                size={16}
                className={generateWgKeysMutation.isPending ? 'animate-spin' : ''}
              />
            </Button>
          </div>
          <Input
            label="Server Public Key"
            {...register('wgPublicKey')}
            className="font-mono text-xs"
          />
          <Input label="MTU" type="number" {...register('wgMTU')} placeholder="1420" />
        </div>
      )}

      {['socks', 'http'].includes(protocol) && (
        <div className="bg-white/5 p-4 rounded-xl border border-white/5 space-y-4">
          <div className="flex items-center justify-between gap-2">
            <h4 className="text-sm font-bold text-secondary uppercase">
              {protocol === 'socks' ? 'SOCKS Auth' : 'HTTP Proxy Auth'}
            </h4>
            <Button
              type="button"
              size="icon"
              variant="secondary"
              onClick={() => generateProxyAuthMutation.mutate()}
            >
              <RefreshCw
                size={16}
                className={generateProxyAuthMutation.isPending ? 'animate-spin' : ''}
              />
            </Button>
          </div>
          <div className="grid grid-cols-1 gap-4">
            <Input
              label="Username"
              {...register('authUser')}
              placeholder="optional"
              autoComplete="new-username"
            />
            <div className="grid grid-cols-[1fr_auto_auto] gap-2 items-end">
              <Input
                label="Password"
                {...register('authPass')}
                type={showAuthPass ? 'text' : 'password'}
                placeholder="optional"
                autoComplete="new-password"
              />
              <Button
                type="button"
                size="icon"
                variant="secondary"
                onClick={() => setShowAuthPass((v) => !v)}
                title={showAuthPass ? 'Hide password' : 'Show password'}
              >
                {showAuthPass ? <EyeOff size={16} /> : <Eye size={16} />}
              </Button>
              <Button
                type="button"
                size="icon"
                variant="secondary"
                onClick={copyAuthPassword}
                title="Copy password"
              >
                <Copy size={16} />
              </Button>
            </div>
          </div>
        </div>
      )}

      <div className="flex justify-end gap-3 pt-2">
        <Button type="button" variant="ghost" onClick={onCancel}>
          Cancel
        </Button>
        <Button type="submit" isLoading={mutation.isPending}>
          Save Inbound
        </Button>
      </div>
    </form>
  );
}
