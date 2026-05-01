import { useEffect, useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import api from '@/lib/api';
import { RoutingProfile, RoutingRule, Outbound, Balancer, OutboundHealth } from '@/lib/types';
import { Button } from '@/components/ui/Button';
import { Modal } from '@/components/ui/Modal';
import { Input } from '@/components/ui/Input';
import { Select } from '@/components/ui/Select';
import { ConfirmationModal } from '@/components/ui/ConfirmationModal';
import {
  Plus,
  Trash2,
  Route,
  Globe,
  ArrowRightLeft,
  Scale,
  Activity,
  Settings2,
  ArrowUp,
  ArrowDown,
  Play,
  Pause,
} from 'lucide-react';
import { toast } from 'react-toastify';
import { motion, AnimatePresence } from 'framer-motion';

type RouteTestInput = {
  domain: string;
  ip: string;
  port: string;
  network: string;
  protocol: string;
  source: string;
  user: string;
  inboundTag: string;
};

type RouteTestResult = {
  matched: boolean;
  index: number;
  target: string;
  comment: string;
};

const DEFAULT_ROUTE_TEST_INPUT: RouteTestInput = {
  domain: '',
  ip: '',
  port: '',
  network: '',
  protocol: '',
  source: '',
  user: '',
  inboundTag: '',
};

const csvList = (value: string): string[] =>
  String(value || '')
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean);

const normalizeText = (value: string): string =>
  String(value || '')
    .trim()
    .toLowerCase();

const matchPort = (inputPort: string, rulePort: string): boolean => {
  const port = Number(inputPort);
  if (!Number.isInteger(port) || port < 1 || port > 65535) return false;

  for (const token of csvList(rulePort)) {
    if (/^\d+$/.test(token) && Number(token) === port) return true;
    const [start, end] = token.split('-').map((item) => Number(item.trim()));
    if (
      Number.isInteger(start) &&
      Number.isInteger(end) &&
      start > 0 &&
      end <= 65535 &&
      start <= end &&
      port >= start &&
      port <= end
    ) {
      return true;
    }
  }
  return false;
};

const matchSimpleStringList = (value: string, list: string[]): boolean => {
  const input = normalizeText(value);
  if (!input) return false;
  return list.some((item) => normalizeText(item) === input);
};

const isIPv4 = (value: string): boolean => {
  const parts = String(value || '')
    .trim()
    .split('.');
  if (parts.length !== 4) return false;
  return parts.every((part) => {
    if (!/^\d+$/.test(part)) return false;
    const n = Number(part);
    return n >= 0 && n <= 255;
  });
};

const ipToInt = (ip: string): number => {
  const [a, b, c, d] = ip.split('.').map(Number);
  return (((a << 24) >>> 0) + (b << 16) + (c << 8) + d) >>> 0;
};

const matchIPv4Cidr = (ip: string, cidr: string): boolean => {
  const [base, bitsRaw] = cidr.split('/');
  const bits = Number(bitsRaw);
  if (!isIPv4(ip) || !isIPv4(base) || !Number.isInteger(bits) || bits < 0 || bits > 32) {
    return false;
  }
  if (bits === 0) return true;
  const mask = (0xffffffff << (32 - bits)) >>> 0;
  return (ipToInt(ip) & mask) === (ipToInt(base) & mask);
};

const matchIpToken = (value: string, token: string): boolean => {
  const input = String(value || '').trim();
  const pattern = String(token || '')
    .trim()
    .toLowerCase();
  if (!input || !pattern || pattern.startsWith('geoip:')) return false;
  if (pattern.includes('/')) return matchIPv4Cidr(input, pattern);
  return normalizeText(input) === normalizeText(pattern);
};

const matchDomainToken = (value: string, token: string): boolean => {
  const domain = normalizeText(value);
  const pattern = normalizeText(token);
  if (!domain || !pattern || pattern.startsWith('geosite:')) return false;
  if (pattern.startsWith('full:')) return domain === pattern.slice(5);
  if (pattern.startsWith('domain:')) {
    const root = pattern.slice(7);
    return domain === root || domain.endsWith(`.${root}`);
  }
  if (pattern.startsWith('keyword:')) return domain.includes(pattern.slice(8));
  return domain === pattern || domain.endsWith(`.${pattern}`);
};

const ruleMatchesInput = (rule: RoutingRule, input: RouteTestInput): boolean => {
  if (rule.enabled === false) return false;

  if ((rule.domain || []).length > 0) {
    if (
      !input.domain ||
      !(rule.domain || []).some((token) => matchDomainToken(input.domain, token))
    ) {
      return false;
    }
  }

  if ((rule.ip || []).length > 0) {
    if (!input.ip || !(rule.ip || []).some((token) => matchIpToken(input.ip, token))) {
      return false;
    }
  }

  if (rule.port && (!input.port || !matchPort(input.port, rule.port))) return false;

  if (rule.network) {
    const networks = csvList(rule.network);
    if (networks.length > 0 && !matchSimpleStringList(input.network, networks)) return false;
  }

  if (
    (rule.protocol || []).length > 0 &&
    !matchSimpleStringList(input.protocol, rule.protocol || [])
  ) {
    return false;
  }

  if ((rule.source || []).length > 0) {
    if (!input.source || !(rule.source || []).some((token) => matchIpToken(input.source, token))) {
      return false;
    }
  }

  if ((rule.user || []).length > 0 && !matchSimpleStringList(input.user, rule.user || [])) {
    return false;
  }

  if (
    (rule.inboundTag || []).length > 0 &&
    !matchSimpleStringList(input.inboundTag, rule.inboundTag || [])
  ) {
    return false;
  }

  return true;
};

const runRouteTest = (rules: RoutingRule[], input: RouteTestInput): RouteTestResult => {
  for (let i = 0; i < rules.length; i += 1) {
    const rule = rules[i];
    if (ruleMatchesInput(rule, input)) {
      return {
        matched: true,
        index: i,
        target: String(rule.outboundTag || rule.balancerTag || 'direct'),
        comment: String(rule.comment || ''),
      };
    }
  }
  return { matched: false, index: -1, target: '', comment: '' };
};

const healthClass = (status: OutboundHealth['status']) => {
  if (status === 'up') return 'border-green-500/30 bg-green-500/10 text-green-400';
  if (status === 'down') return 'border-red-500/30 bg-red-500/10 text-red-400';
  return 'border-gray-500/30 bg-gray-500/10 text-gray-400';
};

const ROUTING_TABS = [
  { id: 'profiles', label: 'Routing Profiles' },
  { id: 'outbounds', label: 'Outbounds' },
  { id: 'balancers', label: 'Balancers' },
] as const;

export default function Routing() {
  const [tab, setTab] = useState<'profiles' | 'outbounds' | 'balancers'>('profiles');

  return (
    <div className="space-y-6 pb-10">
      {/* Pill tab bar */}
      <div className="flex gap-1 bg-white/[0.04] p-1 rounded-2xl border border-white/[0.05] w-fit overflow-x-auto">
        {ROUTING_TABS.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className="relative px-5 py-2 text-xs font-bold uppercase tracking-wider rounded-xl transition-colors whitespace-nowrap"
            style={{ color: tab === t.id ? '#fff' : 'rgba(156,163,175,1)' }}
          >
            {tab === t.id && (
              <motion.div
                layoutId="routingTabPill"
                className="absolute inset-0 bg-gradient-to-br from-primary/25 to-violet-600/20 rounded-xl border border-white/[0.1] shadow-[0_0_12px_rgba(208,188,255,0.12)]"
                transition={{ type: 'spring', stiffness: 500, damping: 35 }}
              />
            )}
            <span className="relative z-10">{t.label}</span>
          </button>
        ))}
      </div>

      {/* Animated content */}
      <AnimatePresence mode="wait">
        <motion.div
          key={tab}
          initial={{ opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -4 }}
          transition={{ duration: 0.16, ease: 'easeOut' }}
        >
          {tab === 'profiles' && <ProfilesView />}
          {tab === 'outbounds' && <OutboundsView />}
          {tab === 'balancers' && <BalancersView />}
        </motion.div>
      </AnimatePresence>
    </div>
  );
}

function ProfilesView() {
  const [modal, setModal] = useState(false);
  const [editingProfile, setEditingProfile] = useState<RoutingProfile | null>(null);
  const [deleteId, setDeleteId] = useState<number | null>(null);

  const { data: profiles, isLoading } = useQuery({
    queryKey: ['routing-profiles'],
    queryFn: async () => (await api.get<RoutingProfile[]>('/routing-profiles')).data,
  });

  const { data: outbounds } = useQuery({
    queryKey: ['outbounds'],
    queryFn: async () => (await api.get<Outbound[]>('/outbounds')).data,
  });

  const { data: balancers } = useQuery({
    queryKey: ['balancers'],
    queryFn: async () => (await api.get<Balancer[]>('/balancers')).data,
  });

  const openCreate = () => {
    setEditingProfile(null);
    setModal(true);
  };
  const openEdit = (p: RoutingProfile) => {
    setEditingProfile(p);
    setModal(true);
  };

  const queryClient = useQueryClient();
  const deleteMutation = useMutation({
    mutationFn: (id: number) => api.delete(`/routing-profiles/${id}`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['routing-profiles'] });
      toast.success('Profile deleted');
      setDeleteId(null);
    },
  });
  const toggleProfileMutation = useMutation({
    mutationFn: ({ id, enable }: { id: number; enable: boolean }) =>
      api.put(`/routing-profiles/${id}`, { enable }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['routing-profiles'] });
      toast.success('Profile status updated');
    },
    onError: (e: any) => toast.error(e.response?.data?.error || 'Failed to update profile'),
  });

  const allTargets = [
    ...(outbounds?.map((o) => o.tag) || ['direct', 'block']),
    ...(balancers?.map((b) => b.tag) || []),
  ];

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <h2 className="text-lg font-bold text-gray-200">Active Profiles</h2>
        <Button onClick={openCreate}>
          <Plus size={16} className="mr-2" /> New Profile
        </Button>
      </div>

      {!isLoading && profiles && profiles.length === 0 && (
        <div className="flex flex-col items-center justify-center py-16 border border-white/5 rounded-3xl bg-[#1a1625]/30">
          <div className="p-4 rounded-full bg-white/5 mb-4">
            <Route size={32} className="text-gray-500" />
          </div>
          <h3 className="text-lg font-bold text-gray-300">No Routing Profiles</h3>
          <p className="text-sm text-gray-500 mt-1">
            Create profiles to manage detailed traffic rules.
          </p>
        </div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
        <AnimatePresence>
          {profiles?.map((p) => (
            <motion.div
              layout
              key={p.id}
              className="bg-surface/40 border border-white/5 rounded-2xl p-6 hover:border-primary/30 transition-colors group relative"
            >
              <div className="flex items-center gap-3 mb-4">
                <div className="p-2 bg-secondary/10 text-secondary rounded-lg">
                  <Route size={24} />
                </div>
                <h3 className="font-bold text-xl">{p.name}</h3>
              </div>
              <div className="space-y-2 mb-6">
                {p.rules.slice(0, 3).map((r, i) => (
                  <div
                    key={i}
                    className="text-xs bg-black/20 p-2 rounded flex justify-between items-center"
                  >
                    <div className="truncate max-w-[60%] font-mono text-gray-400">
                      {r.comment || r.domain?.[0] || r.ip?.[0] || 'Match All'}
                    </div>
                    <span
                      className={`font-bold px-1.5 py-0.5 rounded ${(r.outboundTag || r.balancerTag) === 'block' ? 'bg-red-500/10 text-red-500' : 'bg-primary/10 text-primary'}`}
                    >
                      {r.outboundTag || r.balancerTag || 'direct'}
                    </span>
                  </div>
                ))}
                {p.rules.length > 3 && (
                  <div className="text-center text-xs text-gray-500">
                    +{p.rules.length - 3} more rules
                  </div>
                )}
              </div>
              <div className="flex items-center justify-between mb-2">
                <span
                  className={`text-[10px] uppercase font-bold px-2 py-0.5 rounded ${p.enable !== false ? 'bg-green-500/10 text-green-400' : 'bg-gray-500/10 text-gray-400'}`}
                >
                  {p.enable !== false ? 'Enabled' : 'Disabled'}
                </span>
              </div>
              <div className="flex gap-2">
                <Button className="flex-1 h-10" variant="secondary" onClick={() => openEdit(p)}>
                  Edit Rules
                </Button>
                <Button
                  variant="secondary"
                  className="h-10"
                  onClick={() =>
                    toggleProfileMutation.mutate({ id: p.id, enable: !(p.enable !== false) })
                  }
                  isLoading={toggleProfileMutation.isPending}
                >
                  {p.enable !== false ? (
                    <Pause size={14} className="mr-1" />
                  ) : (
                    <Play size={14} className="mr-1" />
                  )}
                  {p.enable !== false ? 'Disable' : 'Enable'}
                </Button>
                <Button
                  variant="secondary"
                  size="icon"
                  className="h-10 w-10 text-error"
                  onClick={() => setDeleteId(p.id)}
                >
                  <Trash2 size={18} />
                </Button>
              </div>
            </motion.div>
          ))}
        </AnimatePresence>
      </div>

      <Modal
        isOpen={modal}
        onClose={() => setModal(false)}
        title={editingProfile ? 'Edit Profile' : 'New Profile'}
        maxWidth="max-w-7xl"
      >
        <ProfileEditor
          profile={editingProfile}
          onClose={() => setModal(false)}
          outboundOptions={allTargets}
        />
      </Modal>

      <ConfirmationModal
        isOpen={!!deleteId}
        onClose={() => setDeleteId(null)}
        onConfirm={() => deleteId && deleteMutation.mutate(deleteId)}
        title="Delete Profile"
        description="Are you sure you want to delete this routing profile?"
      />
    </div>
  );
}

function ProfileEditor({
  profile,
  onClose,
  outboundOptions,
}: {
  profile: RoutingProfile | null;
  onClose: () => void;
  outboundOptions: string[];
}) {
  const [name, setName] = useState(profile?.name || '');
  const [profileEnabled, setProfileEnabled] = useState(profile?.enable !== false);
  const [rules, setRules] = useState<RoutingRule[]>(
    (profile?.rules || []).map((rule) => ({ ...rule, enabled: rule.enabled !== false }))
  );
  const [testInput, setTestInput] = useState<RouteTestInput>(DEFAULT_ROUTE_TEST_INPUT);
  const [testResult, setTestResult] = useState<RouteTestResult | null>(null);
  const queryClient = useQueryClient();

  useEffect(() => {
    setName(profile?.name || '');
    setProfileEnabled(profile?.enable !== false);
    setRules((profile?.rules || []).map((rule) => ({ ...rule, enabled: rule.enabled !== false })));
    setTestInput(DEFAULT_ROUTE_TEST_INPUT);
    setTestResult(null);
  }, [profile]);

  const mutation = useMutation({
    mutationFn: (data: any) =>
      profile
        ? api.put(`/routing-profiles/${profile.id}`, data)
        : api.post('/routing-profiles', data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['routing-profiles'] });
      toast.success('Saved');
      onClose();
    },
  });

  const addRule = () => {
    setRules([
      ...rules,
      { type: 'field', enabled: true, outboundTag: 'direct', domain: [], ip: [], comment: '' },
    ]);
  };

  const updateRule = (index: number, field: keyof RoutingRule, value: any) => {
    const newRules = [...rules];
    if (['domain', 'ip', 'source', 'protocol', 'user', 'inboundTag'].includes(field)) {
      value = value
        .split(',')
        .map((s: string) => s.trim())
        .filter(Boolean);
    }
    (newRules[index] as any)[field] = value;
    if (field === 'outboundTag') {
      delete (newRules[index] as any).balancerTag;
    }
    setRules(newRules);
  };

  const toggleRuleEnabled = (index: number) => {
    const next = [...rules];
    const current = next[index];
    if (!current) return;
    next[index] = { ...current, enabled: current.enabled === false };
    setRules(next);
  };

  const removeRule = (index: number) => {
    setRules(rules.filter((_, i) => i !== index));
  };

  const moveRule = (index: number, direction: 'up' | 'down') => {
    const targetIndex = direction === 'up' ? index - 1 : index + 1;
    if (targetIndex < 0 || targetIndex >= rules.length) return;

    const next = [...rules];
    const [item] = next.splice(index, 1);
    next.splice(targetIndex, 0, item);
    setRules(next);
  };

  const handleSave = () => {
    if (!name.trim()) return toast.error('Profile Name is required');
    mutation.mutate({
      name: name.trim(),
      enable: profileEnabled,
      rules: rules.map((rule) => ({ ...rule, enabled: rule.enabled !== false })),
    });
  };

  const handleRunTest = () => {
    setTestResult(runRouteTest(rules, testInput));
  };

  const hasGeoPatterns = rules.some((rule) =>
    [...(rule.domain || []), ...(rule.ip || []), ...(rule.source || [])].some((token) => {
      const normalized = String(token || '')
        .trim()
        .toLowerCase();
      return normalized.startsWith('geoip:') || normalized.startsWith('geosite:');
    })
  );

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <Input
          label="Profile Name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          required
        />
        <Select
          label="Profile Status"
          value={profileEnabled ? 'enabled' : 'disabled'}
          onChange={(e) => setProfileEnabled(e.target.value === 'enabled')}
          options={[
            { value: 'enabled', label: 'Enabled' },
            { value: 'disabled', label: 'Disabled' },
          ]}
        />
      </div>
      <div className="space-y-3">
        <div className="flex justify-between items-center">
          <div>
            <h4 className="text-sm font-bold uppercase text-gray-400">Rules</h4>
            <p className="text-[11px] text-gray-500 mt-0.5">Top rule has highest priority.</p>
          </div>
          <Button size="sm" variant="secondary" onClick={addRule}>
            Add Rule
          </Button>
        </div>
        <div className="max-h-[60vh] overflow-y-auto space-y-3 pr-2 custom-scrollbar">
          {rules.map((rule, i) => (
            <div
              key={i}
              className={`bg-white/5 p-4 pt-10 rounded-xl border border-white/5 grid grid-cols-12 gap-4 items-start relative group ${rule.enabled === false ? 'opacity-60' : ''}`}
            >
              <div className="absolute top-2 left-3 flex items-center gap-2">
                <span className="text-xs font-mono text-gray-500">#{i + 1}</span>
                <span
                  className={`text-[10px] uppercase font-bold px-1.5 py-0.5 rounded ${rule.enabled === false ? 'bg-gray-500/20 text-gray-400' : 'bg-green-500/10 text-green-400'}`}
                >
                  {rule.enabled === false ? 'OFF' : 'ON'}
                </span>
              </div>
              <div className="absolute top-2 right-2 flex gap-1 opacity-0 group-hover:opacity-100 transition-opacity z-10">
                <Button
                  variant="icon"
                  size="icon"
                  className="hover:bg-white/10"
                  onClick={() => toggleRuleEnabled(i)}
                  title={rule.enabled === false ? 'Enable rule' : 'Disable rule'}
                >
                  {rule.enabled === false ? <Play size={14} /> : <Pause size={14} />}
                </Button>
                <Button
                  variant="icon"
                  size="icon"
                  className="hover:bg-white/10"
                  disabled={i === 0}
                  onClick={() => moveRule(i, 'up')}
                  title="Move up"
                >
                  <ArrowUp size={14} />
                </Button>
                <Button
                  variant="icon"
                  size="icon"
                  className="hover:bg-white/10"
                  disabled={i === rules.length - 1}
                  onClick={() => moveRule(i, 'down')}
                  title="Move down"
                >
                  <ArrowDown size={14} />
                </Button>
                <Button
                  variant="icon"
                  size="icon"
                  className="text-error hover:bg-white/10"
                  onClick={() => removeRule(i)}
                  title="Delete rule"
                >
                  <Trash2 size={16} />
                </Button>
              </div>

              <div className="col-span-12">
                <Input
                  label="Rule Name / Comment"
                  value={rule.comment || ''}
                  onChange={(e) => updateRule(i, 'comment', e.target.value)}
                  className="bg-black/20"
                  placeholder="Optional note (e.g. Block social media)"
                />
              </div>

              <div className="col-span-12 md:col-span-3 space-y-2">
                <label className="text-[10px] uppercase text-gray-500 font-bold block mb-1">
                  Domains
                </label>
                <textarea
                  className="w-full bg-black/20 text-xs rounded border border-white/10 p-2 h-24 font-mono resize-none"
                  value={rule.domain?.join(', ') || ''}
                  onChange={(e) => updateRule(i, 'domain', e.target.value)}
                  placeholder="geosite:google, domain:com"
                />
              </div>

              <div className="col-span-12 md:col-span-3 space-y-2">
                <label className="text-[10px] uppercase text-gray-500 font-bold block mb-1">
                  IPs
                </label>
                <textarea
                  className="w-full bg-black/20 text-xs rounded border border-white/10 p-2 h-24 font-mono resize-none"
                  value={rule.ip?.join(', ') || ''}
                  onChange={(e) => updateRule(i, 'ip', e.target.value)}
                  placeholder="geoip:cn, 8.8.8.8"
                />
              </div>

              <div className="col-span-12 md:col-span-3 space-y-2">
                <label className="text-[10px] uppercase text-gray-500 font-bold block mb-1">
                  Attributes
                </label>
                <div className="space-y-2">
                  <Input
                    label="Port"
                    value={rule.port || ''}
                    onChange={(e) => updateRule(i, 'port', e.target.value)}
                    className="h-7 text-xs bg-black/20"
                    placeholder="80,443"
                  />
                  <Input
                    label="Network"
                    value={rule.network || ''}
                    onChange={(e) => updateRule(i, 'network', e.target.value)}
                    className="h-7 text-xs bg-black/20"
                    placeholder="tcp,udp"
                  />
                  <Input
                    label="Protocol"
                    value={rule.protocol?.join(',') || ''}
                    onChange={(e) => updateRule(i, 'protocol', e.target.value)}
                    className="h-7 text-xs bg-black/20"
                    placeholder="http,tls"
                  />
                </div>
              </div>

              <div className="col-span-12 md:col-span-3 space-y-2">
                <label className="text-[10px] uppercase text-gray-500 font-bold block mb-1">
                  Source & Target
                </label>
                <div className="space-y-2">
                  <Input
                    label="Source IP"
                    value={rule.source?.join(',') || ''}
                    onChange={(e) => updateRule(i, 'source', e.target.value)}
                    className="h-7 text-xs bg-black/20"
                    placeholder="10.0.0.1"
                  />
                  <Input
                    label="Inbound Tag"
                    value={rule.inboundTag?.join(',') || ''}
                    onChange={(e) => updateRule(i, 'inboundTag', e.target.value)}
                    className="h-7 text-xs bg-black/20"
                    placeholder="inbound tag"
                  />
                  <Input
                    label="User Email"
                    value={rule.user?.join(',') || ''}
                    onChange={(e) => updateRule(i, 'user', e.target.value)}
                    className="h-7 text-xs bg-black/20"
                    placeholder="email1, email2"
                  />

                  <div>
                    <label className="text-[10px] uppercase text-primary font-bold block mb-1 mt-1">
                      Target Outbound
                    </label>
                    <select
                      className="w-full bg-primary/10 text-xs rounded h-8 border border-primary/20 px-2 text-primary font-bold focus:outline-none"
                      value={rule.outboundTag || rule.balancerTag || 'direct'}
                      onChange={(e) => updateRule(i, 'outboundTag', e.target.value)}
                    >
                      {outboundOptions.map((opt) => (
                        <option key={opt} value={opt} className="bg-[#1e1b24]">
                          {opt}
                        </option>
                      ))}
                    </select>
                  </div>
                </div>
              </div>
            </div>
          ))}
        </div>
      </div>

      <div className="bg-white/5 border border-white/5 rounded-xl p-4 space-y-3">
        <div className="flex items-center justify-between">
          <div>
            <h4 className="text-sm font-bold uppercase text-gray-300">Route Tester</h4>
            <p className="text-[11px] text-gray-500 mt-0.5">
              Tests current rule order in editor (first match wins).
            </p>
          </div>
          <Button size="sm" variant="secondary" onClick={handleRunTest}>
            Test Route
          </Button>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-3">
          <Input
            label="Domain"
            value={testInput.domain}
            onChange={(e) => setTestInput({ ...testInput, domain: e.target.value })}
            placeholder="www.example.com"
          />
          <Input
            label="IP"
            value={testInput.ip}
            onChange={(e) => setTestInput({ ...testInput, ip: e.target.value })}
            placeholder="8.8.8.8"
          />
          <Input
            label="Port"
            value={testInput.port}
            onChange={(e) => setTestInput({ ...testInput, port: e.target.value })}
            placeholder="443"
          />
          <Input
            label="Network"
            value={testInput.network}
            onChange={(e) => setTestInput({ ...testInput, network: e.target.value })}
            placeholder="tcp/udp"
          />
          <Input
            label="Protocol"
            value={testInput.protocol}
            onChange={(e) => setTestInput({ ...testInput, protocol: e.target.value })}
            placeholder="http,tls"
          />
          <Input
            label="Source IP"
            value={testInput.source}
            onChange={(e) => setTestInput({ ...testInput, source: e.target.value })}
            placeholder="10.0.0.10"
          />
          <Input
            label="User"
            value={testInput.user}
            onChange={(e) => setTestInput({ ...testInput, user: e.target.value })}
            placeholder="user@example.com"
          />
          <Input
            label="Inbound Tag"
            value={testInput.inboundTag}
            onChange={(e) => setTestInput({ ...testInput, inboundTag: e.target.value })}
            placeholder="inbound tag"
          />
        </div>
        {hasGeoPatterns && (
          <p className="text-xs text-amber-400/90">
            Note: `geoip:` and `geosite:` are not evaluated by local tester.
          </p>
        )}
        {testResult && (
          <div
            className={`rounded-lg border p-3 text-sm ${testResult.matched ? 'border-green-500/30 bg-green-500/10 text-green-300' : 'border-gray-500/20 bg-black/20 text-gray-300'}`}
          >
            {testResult.matched ? (
              <>
                Match: rule #{testResult.index + 1}
                {' -> '}
                <span className="font-bold">{testResult.target}</span>
                {testResult.comment ? ` (${testResult.comment})` : ''}
              </>
            ) : (
              <>No matching rule. Fallback routing will be used.</>
            )}
          </div>
        )}
      </div>

      <div className="flex justify-end">
        <Button onClick={handleSave} isLoading={mutation.isPending}>
          Save Profile
        </Button>
      </div>
    </div>
  );
}

function OutboundsView() {
  const [modal, setModal] = useState(false);
  const [editingOutbound, setEditingOutbound] = useState<Outbound | null>(null);
  const [deleteTag, setDeleteTag] = useState<string | null>(null);

  const { data: outbounds, isLoading } = useQuery({
    queryKey: ['outbounds'],
    queryFn: async () => (await api.get<Outbound[]>('/outbounds')).data,
  });

  const queryClient = useQueryClient();
  const deleteMutation = useMutation({
    mutationFn: (tag: string) => api.delete(`/outbounds/${tag}`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['outbounds'] });
      toast.success('Outbound deleted');
      setDeleteTag(null);
    },
  });
  const toggleOutboundMutation = useMutation({
    mutationFn: ({ tag, enable }: { tag: string; enable: boolean }) =>
      api.put(`/outbounds/${tag}`, { enable }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['outbounds'] });
      queryClient.invalidateQueries({ queryKey: ['balancers'] });
      queryClient.invalidateQueries({ queryKey: ['routing-profiles'] });
      toast.success('Outbound status updated');
    },
    onError: (e: any) => toast.error(e.response?.data?.error || 'Failed to update outbound'),
  });

  const openEdit = (o: Outbound) => {
    setEditingOutbound(o);
    setModal(true);
  };
  const openCreate = () => {
    setEditingOutbound(null);
    setModal(true);
  };

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <h2 className="text-lg font-bold text-gray-200">Outbound Gateways</h2>
        <Button onClick={openCreate}>
          <Plus size={16} className="mr-2" /> New Outbound
        </Button>
      </div>

      {!isLoading && outbounds && outbounds.length === 0 && (
        <div className="flex flex-col items-center justify-center py-16 border border-white/5 rounded-3xl bg-[#1a1625]/30">
          <div className="p-4 rounded-full bg-white/5 mb-4">
            <Globe size={32} className="text-gray-500" />
          </div>
          <h3 className="text-lg font-bold text-gray-300">No Outbounds Found</h3>
          <p className="text-sm text-gray-500 mt-1">Create custom outbounds for routing traffic.</p>
        </div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
        <AnimatePresence>
          {outbounds?.map((o) => {
            const enabled = o.enable !== false;
            return (
              <motion.div
                layout
                key={o.tag}
                className={`bg-surface/40 border rounded-2xl p-6 transition-colors group relative ${o.tag === 'block' ? 'border-red-500/20 bg-red-500/5' : 'border-white/5 hover:border-primary/30'} ${enabled ? '' : 'opacity-60'}`}
              >
                <div className="flex items-center gap-3 mb-4">
                  <div
                    className={`p-2 rounded-lg ${o.tag === 'block' ? 'bg-red-500/10 text-red-500' : 'bg-green-500/10 text-green-500'}`}
                  >
                    {o.protocol === 'freedom' ? (
                      <Globe size={24} />
                    ) : o.protocol === 'blackhole' ? (
                      <Trash2 size={24} />
                    ) : (
                      <ArrowRightLeft size={24} />
                    )}
                  </div>
                  <div>
                    <h3 className="font-bold text-xl">{o.tag}</h3>
                    <p className="text-xs text-gray-500 font-mono uppercase">{o.protocol}</p>
                  </div>
                </div>
                <div className="mb-3">
                  <span
                    className={`text-[10px] uppercase font-bold px-2 py-0.5 rounded ${enabled ? 'bg-green-500/10 text-green-400' : 'bg-gray-500/10 text-gray-400'}`}
                  >
                    {enabled ? 'Enabled' : 'Disabled'}
                  </span>
                </div>

                <div className="text-xs text-gray-400 font-mono bg-black/20 p-3 rounded mb-4 overflow-hidden text-ellipsis whitespace-nowrap">
                  {JSON.stringify(o.settings).slice(0, 50)}...
                </div>

                <div className="flex gap-2 flex-wrap">
                  <Button className="flex-1" variant="secondary" onClick={() => openEdit(o)}>
                    Configure
                  </Button>
                  {o.tag !== 'direct' && o.tag !== 'block' && (
                    <Button
                      variant="secondary"
                      className="h-10"
                      onClick={() =>
                        toggleOutboundMutation.mutate({ tag: o.tag, enable: !enabled })
                      }
                      isLoading={toggleOutboundMutation.isPending}
                    >
                      {enabled ? (
                        <Pause size={14} className="mr-1" />
                      ) : (
                        <Play size={14} className="mr-1" />
                      )}
                      {enabled ? 'Disable' : 'Enable'}
                    </Button>
                  )}
                  {o.tag !== 'direct' && o.tag !== 'block' && (
                    <Button
                      variant="icon"
                      size="icon"
                      className="text-error"
                      onClick={() => setDeleteTag(o.tag)}
                    >
                      <Trash2 size={18} />
                    </Button>
                  )}
                </div>
              </motion.div>
            );
          })}
        </AnimatePresence>
      </div>

      <Modal
        isOpen={modal}
        onClose={() => setModal(false)}
        title={editingOutbound ? `Edit ${editingOutbound.tag}` : 'New Outbound'}
        maxWidth="max-w-2xl"
      >
        <OutboundEditor outbound={editingOutbound} onClose={() => setModal(false)} />
      </Modal>
      <ConfirmationModal
        isOpen={!!deleteTag}
        onClose={() => setDeleteTag(null)}
        onConfirm={() => deleteTag && deleteMutation.mutate(deleteTag)}
        title="Delete Outbound"
        description={`Are you sure you want to permanently delete the outbound gateway "${deleteTag}"? Any routing rules relying on this outbound may fail or stop working.`}
      />
    </div>
  );
}

function OutboundEditor({ outbound, onClose }: { outbound: Outbound | null; onClose: () => void }) {
  const isEdit = !!outbound;
  const [tag, setTag] = useState(outbound?.tag || '');
  const [protocol, setProtocol] = useState(outbound?.protocol || 'freedom');
  const [jsonSettings, setJsonSettings] = useState(
    JSON.stringify(outbound?.settings || {}, null, 2)
  );
  const [jsonStream, setJsonStream] = useState(
    JSON.stringify(outbound?.streamSettings || {}, null, 2)
  );

  const applyTemplate = (type: string) => {
    if (type === 'freedom') {
      setProtocol('freedom');
      setJsonSettings('{}');
      setJsonStream('{}');
    } else if (type === 'blackhole') {
      setProtocol('blackhole');
      setJsonSettings('{}');
      setJsonStream('{}');
    } else if (type === 'socks') {
      setProtocol('socks');
      setJsonSettings(JSON.stringify({ servers: [{ address: '1.1.1.1', port: 1080 }] }, null, 2));
      setJsonStream('{}');
    } else if (type === 'wireguard') {
      setProtocol('wireguard');
      setJsonSettings(
        JSON.stringify(
          { secretKey: 'YOUR_KEY', peers: [{ publicKey: 'PEER_KEY', endpoint: '1.1.1.1:51820' }] },
          null,
          2
        )
      );
      setJsonStream('{}');
    } else if (type === 'vless-reality') {
      setProtocol('vless');
      setJsonSettings(
        JSON.stringify(
          {
            vnext: [
              {
                address: 'example.com',
                port: 443,
                users: [
                  {
                    id: 'YOUR_UUID',
                    encryption: 'none',
                    flow: 'xtls-rprx-vision',
                  },
                ],
              },
            ],
          },
          null,
          2
        )
      );
      setJsonStream(
        JSON.stringify(
          {
            network: 'tcp',
            security: 'reality',
            realitySettings: {
              fingerprint: 'chrome',
              serverName: 'example.com',
              publicKey: 'REALITY_PUB_KEY',
              shortId: 'shortId',
            },
          },
          null,
          2
        )
      );
    } else if (type === 'vmess-ws') {
      setProtocol('vmess');
      setJsonSettings(
        JSON.stringify(
          {
            vnext: [
              {
                address: 'example.com',
                port: 443,
                users: [
                  {
                    id: 'YOUR_UUID',
                    security: 'auto',
                  },
                ],
              },
            ],
          },
          null,
          2
        )
      );
      setJsonStream(
        JSON.stringify(
          {
            network: 'ws',
            security: 'tls',
            wsSettings: {
              path: '/path',
            },
          },
          null,
          2
        )
      );
    } else if (type === 'trojan') {
      setProtocol('trojan');
      setJsonSettings(
        JSON.stringify(
          {
            servers: [
              {
                address: 'example.com',
                port: 443,
                password: 'YOUR_PASSWORD',
              },
            ],
          },
          null,
          2
        )
      );
      setJsonStream(
        JSON.stringify(
          {
            security: 'tls',
          },
          null,
          2
        )
      );
    } else if (type === 'shadowsocks') {
      setProtocol('shadowsocks');
      setJsonSettings(
        JSON.stringify(
          {
            servers: [
              {
                address: '1.1.1.1',
                port: 1234,
                method: '2022-blake3-aes-128-gcm',
                password: 'YOUR_PASSWORD',
                uot: true,
              },
            ],
          },
          null,
          2
        )
      );
      setJsonStream('{}');
    }
  };

  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: (data: any) =>
      isEdit ? api.put(`/outbounds/${outbound.tag}`, data) : api.post('/outbounds', data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['outbounds'] });
      toast.success('Outbound Saved');
      onClose();
    },
    onError: (e: any) => toast.error(e.response?.data?.error || 'Error saving'),
  });

  const handleSave = () => {
    if (!tag.trim()) return toast.error('Outbound Tag is required');
    try {
      const settings = JSON.parse(jsonSettings);
      const streamSettings = JSON.parse(jsonStream);
      mutation.mutate({ tag, protocol, settings, streamSettings });
    } catch (e) {
      toast.error('Invalid JSON format');
    }
  };

  return (
    <div className="space-y-5">
      <div className="grid grid-cols-2 gap-4">
        <Input
          label="Tag (Unique)"
          value={tag}
          onChange={(e) => setTag(e.target.value)}
          disabled={isEdit}
          required
        />
        <Select
          label="Protocol"
          value={protocol}
          onChange={(e) => setProtocol(e.target.value)}
          options={[
            { value: 'freedom', label: 'Freedom (Direct)' },
            { value: 'blackhole', label: 'Blackhole (Block)' },
            { value: 'socks', label: 'Socks Proxy' },
            { value: 'http', label: 'HTTP Proxy' },
            { value: 'wireguard', label: 'WireGuard' },
            { value: 'shadowsocks', label: 'Shadowsocks' },
            { value: 'trojan', label: 'Trojan' },
            { value: 'vless', label: 'VLESS' },
            { value: 'vmess', label: 'VMess' },
          ]}
        />
      </div>

      <div className="space-y-2">
        <label className="text-xs uppercase font-bold text-gray-500">Quick Templates</label>
        <div className="flex flex-wrap gap-2">
          <Button
            size="sm"
            variant="secondary"
            className="text-xs h-7"
            onClick={() => applyTemplate('freedom')}
          >
            Freedom
          </Button>
          <Button
            size="sm"
            variant="secondary"
            className="text-xs h-7"
            onClick={() => applyTemplate('blackhole')}
          >
            Block
          </Button>
          <Button
            size="sm"
            variant="secondary"
            className="text-xs h-7"
            onClick={() => applyTemplate('socks')}
          >
            Socks5
          </Button>
          <Button
            size="sm"
            variant="secondary"
            className="text-xs h-7"
            onClick={() => applyTemplate('wireguard')}
          >
            WireGuard
          </Button>
          <Button
            size="sm"
            variant="secondary"
            className="text-xs h-7"
            onClick={() => applyTemplate('vless-reality')}
          >
            VLESS Reality
          </Button>
          <Button
            size="sm"
            variant="secondary"
            className="text-xs h-7"
            onClick={() => applyTemplate('vmess-ws')}
          >
            VMess WS
          </Button>
          <Button
            size="sm"
            variant="secondary"
            className="text-xs h-7"
            onClick={() => applyTemplate('trojan')}
          >
            Trojan
          </Button>
          <Button
            size="sm"
            variant="secondary"
            className="text-xs h-7"
            onClick={() => applyTemplate('shadowsocks')}
          >
            Shadowsocks
          </Button>
        </div>
      </div>

      <div>
        <label className="text-xs uppercase font-bold text-gray-500 mb-1 block">
          Settings (JSON)
        </label>
        <textarea
          className="w-full bg-black/30 font-mono text-xs p-3 rounded-xl border border-white/10 h-40 focus:border-primary/50 focus:outline-none"
          value={jsonSettings}
          onChange={(e) => setJsonSettings(e.target.value)}
        />
      </div>

      <div>
        <label className="text-xs uppercase font-bold text-gray-500 mb-1 block">
          Stream Settings (JSON)
        </label>
        <textarea
          className="w-full bg-black/30 font-mono text-xs p-3 rounded-xl border border-white/10 h-24 focus:border-primary/50 focus:outline-none"
          value={jsonStream}
          onChange={(e) => setJsonStream(e.target.value)}
        />
      </div>

      <div className="flex justify-end pt-2">
        <Button onClick={handleSave} isLoading={mutation.isPending}>
          <Settings2 size={16} className="mr-2" /> Save Configuration
        </Button>
      </div>
    </div>
  );
}

function BalancersView() {
  const [modal, setModal] = useState(false);
  const [deleteTag, setDeleteTag] = useState<string | null>(null);
  const [editingBalancer, setEditingBalancer] = useState<Balancer | null>(null);

  const { data: balancers, isLoading } = useQuery({
    queryKey: ['balancers'],
    queryFn: async () => (await api.get<Balancer[]>('/balancers')).data,
  });

  const { data: outbounds } = useQuery({
    queryKey: ['outbounds'],
    queryFn: async () => (await api.get<Outbound[]>('/outbounds')).data,
  });
  const { data: outboundHealth, isFetching: isHealthFetching } = useQuery({
    queryKey: ['outbounds-health'],
    queryFn: async () => (await api.get<OutboundHealth[]>('/outbounds/health')).data,
    refetchInterval: 30000,
  });
  const healthMap = new Map<string, OutboundHealth>(
    (outboundHealth || []).map((item) => [item.tag, item])
  );

  const queryClient = useQueryClient();
  const deleteMutation = useMutation({
    mutationFn: (tag: string) => api.delete(`/balancers/${tag}`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['balancers'] });
      toast.success('Balancer deleted');
      setDeleteTag(null);
    },
  });
  const toggleBalancerMutation = useMutation({
    mutationFn: ({ tag, enable }: { tag: string; enable: boolean }) =>
      api.put(`/balancers/${tag}`, { enable }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['balancers'] });
      queryClient.invalidateQueries({ queryKey: ['routing-profiles'] });
      toast.success('Balancer status updated');
    },
    onError: (e: any) => toast.error(e.response?.data?.error || 'Failed to update balancer'),
  });

  const openCreate = () => {
    setEditingBalancer(null);
    setModal(true);
  };
  const openEdit = (balancer: Balancer) => {
    setEditingBalancer(balancer);
    setModal(true);
  };
  const closeModal = () => {
    setModal(false);
    setEditingBalancer(null);
  };

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <h2 className="text-lg font-bold text-gray-200">Load Balancers</h2>
        <Button onClick={openCreate}>
          <Plus size={16} className="mr-2" /> New Balancer
        </Button>
      </div>

      {!isLoading && balancers && balancers.length === 0 && (
        <div className="flex flex-col items-center justify-center py-16 border border-white/5 rounded-3xl bg-[#1a1625]/30">
          <div className="p-4 rounded-full bg-white/5 mb-4">
            <Scale size={32} className="text-gray-500" />
          </div>
          <h3 className="text-lg font-bold text-gray-300">No Balancers Found</h3>
          <p className="text-sm text-gray-500 mt-1">
            Create a balancer to distribute traffic across multiple outbounds.
          </p>
        </div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
        <AnimatePresence>
          {balancers?.map((b) => {
            const enabled = b.enable !== false;
            const selectorHealth = b.selector
              .map((tag) => healthMap.get(tag))
              .filter((item): item is OutboundHealth => !!item);
            const healthy = selectorHealth.filter((item) => item.status === 'up');
            const avgRtt =
              healthy.length > 0
                ? Math.round(
                    healthy.reduce((acc, item) => acc + (item.rttMs || 0), 0) / healthy.length
                  )
                : null;

            return (
              <motion.div
                layout
                key={b.tag}
                className={`bg-surface/40 border border-white/5 rounded-2xl p-6 hover:border-primary/30 transition-colors group relative ${enabled ? '' : 'opacity-60'}`}
              >
                <div className="flex items-center gap-3 mb-4">
                  <div className="p-2 rounded-lg bg-indigo-500/10 text-indigo-400">
                    <Scale size={24} />
                  </div>
                  <div>
                    <h3 className="font-bold text-xl">{b.tag}</h3>
                    <div className="flex items-center gap-2">
                      <p className="text-xs text-gray-500 font-mono uppercase">{b.strategy}</p>
                      {b.strategy === 'leastPing' && (
                        <Activity size={12} className="text-green-500" />
                      )}
                    </div>
                    <div className="text-[10px] text-gray-500 mt-1">
                      {isHealthFetching
                        ? 'Checking health...'
                        : `Healthy ${healthy.length}/${b.selector.length}${avgRtt !== null ? ` • avg ${avgRtt}ms` : ''}`}
                    </div>
                  </div>
                </div>
                <div className="mb-3">
                  <span
                    className={`text-[10px] uppercase font-bold px-2 py-0.5 rounded ${enabled ? 'bg-green-500/10 text-green-400' : 'bg-gray-500/10 text-gray-400'}`}
                  >
                    {enabled ? 'Enabled' : 'Disabled'}
                  </span>
                </div>
                <div className="space-y-2 mb-4">
                  <div className="text-xs uppercase font-bold text-gray-500">Selectors</div>
                  <div className="flex flex-wrap gap-2">
                    {b.selector.map((s) => (
                      <span
                        key={s}
                        className={`px-2 py-1 rounded text-xs font-mono border ${healthClass(healthMap.get(s)?.status || 'unknown')}`}
                        title={healthMap.get(s)?.endpoint || ''}
                      >
                        {s}
                        {healthMap.get(s)?.rttMs ? ` • ${healthMap.get(s)?.rttMs}ms` : ''}
                      </span>
                    ))}
                  </div>
                </div>
                {b.fallback_tag && (
                  <div className="space-y-2 mb-4">
                    <div className="text-xs uppercase font-bold text-gray-500">Fallback</div>
                    <span
                      className={`px-2 py-1 rounded text-xs font-mono border ${
                        b.fallback_tag === 'direct'
                          ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30'
                          : b.fallback_tag === 'block'
                            ? 'bg-red-500/10 text-red-400 border-red-500/30'
                            : 'bg-white/[0.06] text-gray-300 border-white/10'
                      }`}
                    >
                      {b.fallback_tag}
                    </span>
                  </div>
                )}
                <div className="flex justify-end gap-2 flex-wrap">
                  <Button variant="secondary" size="sm" onClick={() => openEdit(b)}>
                    <Settings2 size={14} className="mr-1" /> Edit
                  </Button>
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => toggleBalancerMutation.mutate({ tag: b.tag, enable: !enabled })}
                    isLoading={toggleBalancerMutation.isPending}
                  >
                    {enabled ? (
                      <Pause size={14} className="mr-1" />
                    ) : (
                      <Play size={14} className="mr-1" />
                    )}
                    {enabled ? 'Disable' : 'Enable'}
                  </Button>
                  <Button
                    variant="icon"
                    size="icon"
                    className="text-error"
                    onClick={() => setDeleteTag(b.tag)}
                  >
                    <Trash2 size={18} />
                  </Button>
                </div>
              </motion.div>
            );
          })}
        </AnimatePresence>
      </div>

      <Modal
        isOpen={modal}
        onClose={closeModal}
        title={editingBalancer ? `Edit Balancer: ${editingBalancer.tag}` : 'New Balancer'}
        maxWidth="max-w-xl"
      >
        <BalancerEditor
          balancer={editingBalancer}
          onClose={closeModal}
          outbounds={outbounds || []}
          healthMap={healthMap}
          healthLoading={isHealthFetching}
        />
      </Modal>
      <ConfirmationModal
        isOpen={!!deleteTag}
        onClose={() => setDeleteTag(null)}
        onConfirm={() => deleteTag && deleteMutation.mutate(deleteTag)}
        title="Delete Balancer"
        description={`Delete balancer "${deleteTag}"?`}
      />
    </div>
  );
}

function BalancerEditor({
  balancer,
  onClose,
  outbounds,
  healthMap,
  healthLoading,
}: {
  balancer?: Balancer | null;
  onClose: () => void;
  outbounds: Outbound[];
  healthMap: Map<string, OutboundHealth>;
  healthLoading: boolean;
}) {
  const isEdit = !!balancer;
  const [tag, setTag] = useState(balancer?.tag || '');
  const [strategy, setStrategy] = useState(balancer?.strategy || 'random');
  const [selected, setSelected] = useState<string[]>(balancer?.selector || []);
  const [fallbackTag, setFallbackTag] = useState<string>(balancer?.fallback_tag || '');

  useEffect(() => {
    setTag(balancer?.tag || '');
    setStrategy(balancer?.strategy || 'random');
    setSelected(balancer?.selector || []);
    setFallbackTag(balancer?.fallback_tag || '');
  }, [balancer]);

  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: (data: any) =>
      isEdit ? api.put(`/balancers/${balancer!.tag}`, data) : api.post('/balancers', data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['balancers'] });
      toast.success(isEdit ? 'Balancer updated' : 'Balancer created');
      onClose();
    },
    onError: (e: any) => toast.error(e.response?.data?.error),
  });

  const toggleOutbound = (t: string) => {
    if (selected.includes(t)) {
      setSelected(selected.filter((s) => s !== t));
    } else {
      setSelected([...selected, t]);
    }
  };

  const handleSave = () => {
    if (!isEdit && !tag) return toast.error('Tag required');
    if (selected.length < 1) return toast.error('Select at least one outbound');
    if (fallbackTag && selected.includes(fallbackTag)) {
      return toast.error('Fallback cannot be one of the selected outbounds');
    }
    const fallbackPayload = fallbackTag || null;
    if (isEdit) {
      mutation.mutate({ selector: selected, strategy, fallback_tag: fallbackPayload });
      return;
    }
    mutation.mutate({ tag, selector: selected, strategy, fallback_tag: fallbackPayload });
  };

  const selectedHealth = selected
    .map((item) => healthMap.get(item))
    .filter((item): item is OutboundHealth => !!item);
  const healthyCount = selectedHealth.filter((item) => item.status === 'up').length;
  const averageRtt =
    healthyCount > 0
      ? Math.round(
          selectedHealth
            .filter((item) => item.status === 'up')
            .reduce((sum, item) => sum + (item.rttMs || 0), 0) / healthyCount
        )
      : null;

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 gap-4">
        <Input
          label="Balancer Tag"
          value={tag}
          onChange={(e) => setTag(e.target.value)}
          placeholder="load-balancer"
          disabled={isEdit}
        />
        <Select
          label="Strategy"
          value={strategy}
          onChange={(e) => setStrategy(e.target.value)}
          options={[
            { value: 'random', label: 'Random (Equal)' },
            { value: 'leastLoad', label: 'Least Load' },
            { value: 'leastPing', label: 'Least Ping' },
          ]}
        />
      </div>

      <div className="rounded-xl border border-white/10 bg-black/20 p-3 text-xs text-gray-400">
        {healthLoading
          ? 'Checking outbound health...'
          : `Selected healthy: ${healthyCount}/${selected.length}${averageRtt !== null ? ` • avg RTT ${averageRtt}ms` : ''}`}
      </div>

      <div className="space-y-2">
        <label className="text-xs uppercase font-bold text-gray-500">Select Outbounds</label>
        <div className="grid grid-cols-2 gap-2 max-h-60 overflow-y-auto custom-scrollbar">
          {outbounds
            .filter((o) => o.tag !== 'direct' && o.tag !== 'block')
            .map((o) => (
              <div
                key={o.tag}
                onClick={() => toggleOutbound(o.tag)}
                className={`p-3 rounded-xl border cursor-pointer transition-all ${selected.includes(o.tag) ? 'bg-primary/20 border-primary text-white' : 'bg-black/20 border-white/5 text-gray-400 hover:bg-white/5'}`}
              >
                <div className="font-bold text-sm">{o.tag}</div>
                <div className="flex items-center justify-between mt-1">
                  <div className="text-[10px] uppercase opacity-70">{o.protocol}</div>
                  <span
                    className={`text-[10px] px-1.5 py-0.5 rounded border ${healthClass(healthMap.get(o.tag)?.status || 'unknown')}`}
                  >
                    {healthMap.get(o.tag)?.status || 'unknown'}
                    {healthMap.get(o.tag)?.rttMs ? ` • ${healthMap.get(o.tag)?.rttMs}ms` : ''}
                  </span>
                </div>
              </div>
            ))}
        </div>
      </div>

      <div className="space-y-2">
        <label className="text-xs uppercase font-bold text-gray-500">Fallback Outbound</label>
        <Select
          value={fallbackTag}
          onChange={(e) => setFallbackTag(e.target.value)}
          options={[
            { value: '', label: '— None —' },
            ...outbounds.map((o) => ({ value: o.tag, label: o.tag })),
          ]}
        />
        {fallbackTag && selected.includes(fallbackTag) && (
          <p className="text-xs text-red-400">Fallback cannot be one of the selected outbounds.</p>
        )}
        <p className="text-xs text-gray-500">
          Outbound used when all selector nodes are unreachable. Use <code>direct</code> to bypass
          proxy, <code>block</code> to drop traffic.
        </p>
      </div>

      <div className="flex justify-end">
        <Button onClick={handleSave} isLoading={mutation.isPending}>
          {isEdit ? 'Save Changes' : 'Create Balancer'}
        </Button>
      </div>
    </div>
  );
}
