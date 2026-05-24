import { useState, useEffect } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import api from '@/lib/api';
import { Node, MasterInfo } from '@/lib/types';
import { Button } from '@/components/ui/Button';
import { Modal } from '@/components/ui/Modal';
import { Input } from '@/components/ui/Input';
import { Switch } from '@/components/ui/Switch';
import { TagInput } from '@/components/ui/TagInput';
import { ConfirmationModal } from '@/components/ui/ConfirmationModal';
import { formatDateTime } from '@/lib/datetime';
import {
  Plus,
  Trash2,
  Server,
  Pencil,
  Wifi,
  WifiOff,
  HelpCircle,
  RefreshCw,
  Zap,
  Upload,
  UploadCloud,
} from 'lucide-react';
import { toast } from 'react-toastify';
import { motion, AnimatePresence } from 'framer-motion';

const STATUS_ICON: Record<string, { icon: typeof Wifi; color: string; label: string }> = {
  online: { icon: Wifi, color: 'text-emerald-400', label: 'Online' },
  offline: { icon: WifiOff, color: 'text-red-400', label: 'Offline' },
  unknown: { icon: HelpCircle, color: 'text-gray-500', label: 'Unknown' },
};

type NodeFormData = {
  name: string;
  url: string;
  username: string;
  password: string;
  inbound_tag: string;
  enable: boolean;
  sync_users: boolean;
  sync_inbound: boolean;
  strict_mirror: boolean;
  groups: string[];
};

const EMPTY_FORM: NodeFormData = {
  name: '',
  url: '',
  username: '',
  password: '',
  inbound_tag: '',
  enable: true,
  sync_users: true,
  sync_inbound: true,
  strict_mirror: false,
  groups: [],
};

function formatLastCheck(ts: number): string {
  if (!ts) return 'Never';
  const diff = Date.now() - ts;
  if (diff < 60000) return `${Math.floor(diff / 1000)}s ago`;
  if (diff < 3600000) return `${Math.floor(diff / 60000)}m ago`;
  return formatDateTime(ts);
}

export default function Nodes() {
  const queryClient = useQueryClient();
  const [showForm, setShowForm] = useState(false);
  const [editingNode, setEditingNode] = useState<Node | null>(null);
  const [formData, setFormData] = useState<NodeFormData>(EMPTY_FORM);
  const [deleteTarget, setDeleteTarget] = useState<Node | null>(null);
  const [showMasterForm, setShowMasterForm] = useState(false);
  const [masterDraftTags, setMasterDraftTags] = useState<string[]>([]);
  const [forcePushTarget, setForcePushTarget] = useState<Node | null>(null);
  const [, setTick] = useState(0);

  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(id);
  }, []);

  const { data: nodes = [], isLoading } = useQuery<Node[]>({
    queryKey: ['nodes'],
    queryFn: () => api.get('/nodes').then((r) => r.data),
    refetchInterval: 30000,
  });

  const { data: master } = useQuery<MasterInfo>({
    queryKey: ['nodes', 'master'],
    queryFn: () => api.get('/nodes/master').then((r) => r.data),
  });

  const masterMutation = useMutation({
    mutationFn: (groups: string[]) => api.put('/nodes/master', { groups }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['nodes', 'master'] });
      setShowMasterForm(false);
      toast.success('Master tags updated');
    },
    onError: (err: any) => {
      toast.error(err?.response?.data?.error || 'Failed to update master tags');
    },
  });

  const saveMutation = useMutation({
    mutationFn: (data: { id?: number; form: NodeFormData }) =>
      data.id ? api.put(`/nodes/${data.id}`, data.form) : api.post('/nodes', data.form),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['nodes'] });
      setShowForm(false);
      setEditingNode(null);
      toast.success(editingNode ? 'Node updated' : 'Node added');
    },
    onError: (err: any) => {
      toast.error(err?.response?.data?.error || 'Failed to save node');
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: number) => api.delete(`/nodes/${id}`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['nodes'] });
      setDeleteTarget(null);
      toast.success('Node deleted');
    },
    onError: () => toast.error('Failed to delete node'),
  });

  const testMutation = useMutation({
    mutationFn: (id: number) => api.post(`/nodes/${id}/test`),
    onSuccess: (res) => {
      queryClient.invalidateQueries({ queryKey: ['nodes'] });
      const data = res.data;
      if (data.online) {
        toast.success(`Connection OK (${data.latency_ms}ms)`);
      } else {
        toast.error(`Connection failed: ${data.error || 'Unknown error'}`);
      }
    },
    onError: () => toast.error('Test request failed'),
  });

  const syncMutation = useMutation({
    mutationFn: () => api.post('/nodes/sync'),
    onSuccess: () => {
      toast.success('User sync completed');
    },
    onError: () => toast.error('Sync failed'),
  });

  const formatPushResult = (res: any, forced: boolean) => {
    const u = res?.data?.users;
    const written = res?.data?.inbound_written;
    const inboundPart = written
      ? forced
        ? 'inbound overwritten'
        : 'inbound created'
      : 'inbound left as-is';
    const userPart =
      u && (u.added || u.updated || u.deleted || u.failed)
        ? `users +${u.added} / ~${u.updated} / -${u.deleted}` +
          (u.failed ? ` (${u.failed} failed)` : '')
        : 'users in sync';
    return `${inboundPart}; ${userPart}`;
  };

  const pushConfigMutation = useMutation({
    mutationFn: (id: number) => api.post(`/nodes/${id}/sync-inbound`),
    onSuccess: (res: any) => {
      toast.success(formatPushResult(res, false));
    },
    onError: (err: any) => {
      toast.error(err?.response?.data?.error || 'Push failed');
    },
  });

  const forcePushMutation = useMutation({
    mutationFn: (id: number) => api.post(`/nodes/${id}/sync-inbound?force=1`),
    onSuccess: (res: any) => {
      setForcePushTarget(null);
      toast.success(formatPushResult(res, true));
    },
    onError: (err: any) => {
      toast.error(err?.response?.data?.error || 'Force push failed');
    },
  });

  const openAdd = () => {
    setEditingNode(null);
    setFormData(EMPTY_FORM);
    setShowForm(true);
  };

  const openEdit = (node: Node) => {
    setEditingNode(node);
    setFormData({
      name: node.name,
      url: node.url,
      username: node.username,
      password: '',
      inbound_tag: node.inbound_tag,
      enable: node.enable,
      sync_users: node.sync_users,
      sync_inbound: node.sync_inbound,
      strict_mirror: !!node.strict_mirror,
      groups: node.groups || [],
    });
    setShowForm(true);
  };

  const handleSubmit = () => {
    const payload = { ...formData };
    if (editingNode && !payload.password) {
      payload.password = '••••••••';
    }
    saveMutation.mutate({ id: editingNode?.id, form: payload });
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-white">Nodes</h1>
        <div className="flex gap-2">
          <Button
            variant="secondary"
            onClick={() => syncMutation.mutate()}
            disabled={syncMutation.isPending}
          >
            <RefreshCw size={16} className={syncMutation.isPending ? 'animate-spin' : ''} />
            <span className="ml-1.5">Sync All</span>
          </Button>
          <Button onClick={openAdd}>
            <Plus size={16} />
            <span className="ml-1.5">Add Node</span>
          </Button>
        </div>
      </div>

      {/* Master panel card — represents the local instance, tags here gate which
          users can see master inbounds in their subscription. */}
      <div className="glass-card p-5 rounded-2xl border border-white/[0.06] space-y-3">
        <div className="flex items-start justify-between gap-3">
          <div className="flex items-center gap-3 min-w-0">
            <div className="p-2 rounded-xl bg-primary/10">
              <Server size={20} className="text-primary" />
            </div>
            <div className="min-w-0">
              <h3 className="font-semibold text-white truncate">Master (this panel)</h3>
              <p className="text-xs text-gray-500">
                Tags here behave like node tags — users with Allowed Node Tags must overlap to see
                master inbounds. Empty = visible to everyone.
              </p>
            </div>
          </div>
          <Button
            variant="secondary"
            size="sm"
            onClick={() => {
              setMasterDraftTags(master?.groups || []);
              setShowMasterForm(true);
            }}
          >
            <Pencil size={14} />
            <span className="ml-1">Edit Tags</span>
          </Button>
        </div>
        <div className="flex flex-wrap gap-1.5">
          {(master?.groups || []).length === 0 ? (
            <span className="text-xs text-gray-500 italic">No tags — visible to all users</span>
          ) : (
            (master?.groups || []).map((g) => (
              <span
                key={g}
                className="inline-flex items-center rounded-md bg-primary/15 border border-primary/30 px-2 py-0.5 text-[11px] font-medium text-primary"
              >
                {g}
              </span>
            ))
          )}
        </div>
      </div>

      {isLoading ? (
        <div className="text-center text-gray-500 py-12">Loading...</div>
      ) : nodes.length === 0 ? (
        <div className="text-center text-gray-500 py-12">
          <Server size={48} className="mx-auto mb-4 opacity-30" />
          <p>No nodes configured</p>
          <p className="text-sm mt-1">Add a remote panel node to enable multi-server management</p>
        </div>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          <AnimatePresence mode="popLayout">
            {nodes.map((node, i) => {
              const st = STATUS_ICON[node.status] || STATUS_ICON.unknown;
              const StatusIcon = st.icon;
              return (
                <motion.div
                  key={node.id}
                  initial={{ opacity: 0, y: 20 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0, scale: 0.95 }}
                  transition={{ delay: i * 0.05 }}
                  className="glass-card p-5 rounded-2xl border border-white/[0.06] space-y-4"
                >
                  <div className="flex items-start justify-between">
                    <div className="flex items-center gap-3 min-w-0">
                      <div
                        className={`p-2 rounded-xl ${node.enable ? 'bg-primary/10' : 'bg-gray-800'}`}
                      >
                        <Server
                          size={20}
                          className={node.enable ? 'text-primary' : 'text-gray-600'}
                        />
                      </div>
                      <div className="min-w-0">
                        <h3 className="font-semibold text-white truncate">{node.name}</h3>
                        <p className="text-xs text-gray-500 truncate" title={node.url}>
                          {node.url}
                        </p>
                      </div>
                    </div>
                    <div className="flex items-center gap-1.5 shrink-0">
                      <StatusIcon size={16} className={st.color} />
                      <span className={`text-xs font-medium ${st.color}`}>{st.label}</span>
                    </div>
                  </div>

                  <div className="grid grid-cols-2 gap-2 text-xs">
                    <div className="bg-white/[0.03] rounded-lg px-3 py-2">
                      <span className="text-gray-500">Inbound</span>
                      <p className="text-gray-300 font-mono truncate">{node.inbound_tag}</p>
                    </div>
                    <div className="bg-white/[0.03] rounded-lg px-3 py-2">
                      <span className="text-gray-500">Last Check</span>
                      <p className="text-gray-300">{formatLastCheck(node.last_check)}</p>
                    </div>
                  </div>

                  {node.groups && node.groups.length > 0 && (
                    <div className="flex flex-wrap gap-1.5">
                      {node.groups.map((g) => (
                        <span
                          key={g}
                          className="inline-flex items-center rounded-md bg-primary/15 border border-primary/30 px-2 py-0.5 text-[11px] font-medium text-primary"
                        >
                          {g}
                        </span>
                      ))}
                    </div>
                  )}

                  {!node.enable && (
                    <div className="text-xs text-yellow-500/80 bg-yellow-500/10 rounded-lg px-3 py-1.5">
                      Disabled
                    </div>
                  )}

                  {node.last_error && node.status === 'offline' && (
                    <div
                      className="text-xs text-red-400/80 bg-red-500/10 rounded-lg px-3 py-1.5 truncate"
                      title={node.last_error}
                    >
                      {node.last_error}
                    </div>
                  )}

                  <div className="flex gap-2 pt-1">
                    <Button
                      variant="secondary"
                      size="sm"
                      onClick={() => testMutation.mutate(node.id)}
                      disabled={testMutation.isPending}
                    >
                      <Zap size={14} />
                      <span className="ml-1">Test</span>
                    </Button>
                    <Button
                      variant="secondary"
                      size="sm"
                      onClick={() => pushConfigMutation.mutate(node.id)}
                      disabled={pushConfigMutation.isPending || !node.enable}
                      title="Create the inbound on this node if missing and reconcile users. Existing remote inbound is left untouched."
                    >
                      <Upload size={14} />
                      <span className="ml-1">Push</span>
                    </Button>
                    <Button
                      variant="secondary"
                      size="sm"
                      onClick={() => setForcePushTarget(node)}
                      disabled={forcePushMutation.isPending || !node.enable}
                      title="Force overwrite the remote inbound from master and reconcile users. Irreversible."
                    >
                      <UploadCloud size={14} />
                      <span className="ml-1">Force</span>
                    </Button>
                    <Button variant="secondary" size="sm" onClick={() => openEdit(node)}>
                      <Pencil size={14} />
                      <span className="ml-1">Edit</span>
                    </Button>
                    <Button variant="danger" size="sm" onClick={() => setDeleteTarget(node)}>
                      <Trash2 size={14} />
                    </Button>
                  </div>
                </motion.div>
              );
            })}
          </AnimatePresence>
        </div>
      )}

      {/* Add/Edit Modal */}
      <Modal
        isOpen={showForm}
        onClose={() => {
          setShowForm(false);
          setEditingNode(null);
        }}
        title={editingNode ? 'Edit Node' : 'Add Node'}
      >
        <div className="space-y-4">
          <Input
            label="Name"
            placeholder="e.g. Frankfurt-1"
            value={formData.name}
            onChange={(e) => setFormData({ ...formData, name: e.target.value })}
          />
          <Input
            label="URL"
            placeholder="https://node.example.com/secret-path"
            value={formData.url}
            onChange={(e) => setFormData({ ...formData, url: e.target.value })}
          />
          <div className="grid grid-cols-2 gap-3">
            <Input
              label="Username"
              name="node-username"
              autoComplete="off"
              value={formData.username}
              onChange={(e) => setFormData({ ...formData, username: e.target.value })}
            />
            <Input
              label="Password"
              type="password"
              name="node-password"
              autoComplete="new-password"
              placeholder={editingNode ? '(unchanged)' : ''}
              value={formData.password}
              onChange={(e) => setFormData({ ...formData, password: e.target.value })}
            />
          </div>
          <Input
            label="Inbound Tag"
            placeholder="Target inbound tag on remote node"
            value={formData.inbound_tag}
            onChange={(e) => setFormData({ ...formData, inbound_tag: e.target.value })}
          />
          <TagInput
            label="Tags"
            value={formData.groups}
            onChange={(tags) => setFormData({ ...formData, groups: tags })}
            placeholder="e.g. free, eu, premium"
            helperText="Used to grant users access to specific nodes via Allowed Node Tags"
          />
          <div className="flex flex-wrap gap-6">
            <Switch
              label="Enabled"
              checked={formData.enable}
              onChange={(e) => setFormData({ ...formData, enable: e.target.checked })}
            />
            <Switch
              label="Sync Users"
              checked={formData.sync_users}
              onChange={(e) => setFormData({ ...formData, sync_users: e.target.checked })}
            />
            <Switch
              label="Sync Inbound"
              checked={formData.sync_inbound}
              onChange={(e) => setFormData({ ...formData, sync_inbound: e.target.checked })}
            />
            <Switch
              label="Strict Mirror"
              checked={formData.strict_mirror}
              onChange={(e) => setFormData({ ...formData, strict_mirror: e.target.checked })}
            />
          </div>
          <div className="flex justify-end gap-2 pt-2">
            <Button
              variant="secondary"
              onClick={() => {
                setShowForm(false);
                setEditingNode(null);
              }}
            >
              Cancel
            </Button>
            <Button onClick={handleSubmit} disabled={saveMutation.isPending}>
              {saveMutation.isPending ? 'Saving...' : editingNode ? 'Update' : 'Add'}
            </Button>
          </div>
        </div>
      </Modal>

      {/* Master tags edit modal */}
      <Modal
        isOpen={showMasterForm}
        onClose={() => setShowMasterForm(false)}
        title="Master Panel Tags"
      >
        <div className="space-y-4">
          <TagInput
            label="Tags"
            value={masterDraftTags}
            onChange={setMasterDraftTags}
            placeholder="e.g. core, admin"
            helperText="Empty = master is visible to every user. With tags, only users whose Allowed Node Tags overlap will see master inbounds."
          />
          <div className="flex justify-end gap-2 pt-2">
            <Button variant="secondary" onClick={() => setShowMasterForm(false)}>
              Cancel
            </Button>
            <Button
              onClick={() => masterMutation.mutate(masterDraftTags)}
              disabled={masterMutation.isPending}
            >
              {masterMutation.isPending ? 'Saving...' : 'Save'}
            </Button>
          </div>
        </div>
      </Modal>

      {/* Delete Confirmation */}
      <ConfirmationModal
        isOpen={!!deleteTarget}
        onClose={() => setDeleteTarget(null)}
        onConfirm={() => deleteTarget && deleteMutation.mutate(deleteTarget.id)}
        title="Delete Node"
        description={`Remove node "${deleteTarget?.name}"? This won't affect users on the remote panel.`}
        confirmText="Delete"
        isLoading={deleteMutation.isPending}
      />

      {/* Force Push Confirmation */}
      <ConfirmationModal
        isOpen={!!forcePushTarget}
        onClose={() => setForcePushTarget(null)}
        onConfirm={() => forcePushTarget && forcePushMutation.mutate(forcePushTarget.id)}
        title="Force Push Inbound"
        description={`This will OVERWRITE the inbound "${forcePushTarget?.inbound_tag}" on node "${forcePushTarget?.name}" with the master config. Any node-side tweaks will be lost. This action is irreversible. Continue?`}
        confirmText="Force Push"
        isLoading={forcePushMutation.isPending}
      />
    </div>
  );
}
