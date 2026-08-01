import { useState, useEffect } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import api from '@ui/lib/api';
import { LinkedPanel, Inbound } from '@ui/lib/types';
import { Button } from '@ui/components/ui/Button';
import { Modal } from '@ui/components/ui/Modal';
import { Input } from '@ui/components/ui/Input';
import { Switch } from '@ui/components/ui/Switch';
import { ConfirmationModal } from '@ui/components/ui/ConfirmationModal';
import { formatDateTime } from '@ui/lib/datetime';
import {
  Plus,
  Trash2,
  Server,
  Pencil,
  Wifi,
  WifiOff,
  History,
  HelpCircle,
  Zap,
  Shield,
  Layers,
  Clock,
  Link2,
  Download,
  Upload,
} from 'lucide-react';
import { toast } from 'react-toastify';
import { motion, AnimatePresence } from 'framer-motion';

const STATUS_ICON: Record<string, { icon: typeof Wifi; color: string; label: string }> = {
  online: { icon: Wifi, color: 'text-emerald-400', label: 'Online' },
  offline: { icon: WifiOff, color: 'text-red-400', label: 'Offline' },
  stale: { icon: History, color: 'text-amber-400', label: 'Stale' },
  unknown: { icon: HelpCircle, color: 'text-gray-500', label: 'Unknown' },
};

type AddPanelForm = {
  name: string;
  link_token: string;
};

const EMPTY_ADD_FORM: AddPanelForm = {
  name: '',
  link_token: '',
};

const MAX_RESTORE_FILE_BYTES = 50 * 1024 * 1024;
const ALLOWED_RESTORE_EXTENSIONS = ['.db', '.sqlite', '.sqlite3'];

type EditPanelForm = {
  name: string;
  enable: boolean;
};

function formatLastPoll(ts: number | null): string {
  if (!ts) return 'Never';
  const diff = Date.now() - ts;
  if (diff < 0) return 'Just now';
  if (diff < 60000) return `${Math.floor(diff / 1000)}s ago`;
  if (diff < 3600000) return `${Math.floor(diff / 60000)}m ago`;
  return formatDateTime(ts);
}

export default function Panels() {
  const queryClient = useQueryClient();

  const [showAddForm, setShowAddForm] = useState(false);
  const [addFormData, setAddFormData] = useState<AddPanelForm>(EMPTY_ADD_FORM);
  const [editingPanel, setEditingPanel] = useState<LinkedPanel | null>(null);
  const [editFormData, setEditFormData] = useState<EditPanelForm>({ name: '', enable: true });
  const [unlinkTarget, setUnlinkTarget] = useState<LinkedPanel | null>(null);
  const [relinkTarget, setRelinkTarget] = useState<LinkedPanel | null>(null);
  const [relinkToken, setRelinkToken] = useState('');
  const [backupBusyId, setBackupBusyId] = useState<number | null>(null);
  const [restoreTarget, setRestoreTarget] = useState<LinkedPanel | null>(null);
  const [restoreFile, setRestoreFile] = useState<File | null>(null);
  const [restoreConfirmName, setRestoreConfirmName] = useState('');
  const [, setTick] = useState(0);

  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(id);
  }, []);

  const { data: panels = [], isLoading: panelsLoading } = useQuery<LinkedPanel[]>({
    queryKey: ['panels'],
    queryFn: () => api.get('/panels').then((r) => r.data),
    refetchInterval: 10000,
    enabled: true,
  });

  const { data: inbounds = [] } = useQuery<Inbound[]>({
    queryKey: ['inbounds'],
    queryFn: () => api.get('/inbounds').then((r) => r.data),
    enabled: true,
  });

  const addPanelMutation = useMutation({
    mutationFn: (data: AddPanelForm) => api.post('/panels', data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['panels'] });
      setShowAddForm(false);
      setAddFormData(EMPTY_ADD_FORM);
      toast.success('Panel linked successfully');
    },
    onError: (err: any) => {
      toast.error(err?.response?.data?.error || 'Failed to link panel');
    },
  });

  const updatePanelMutation = useMutation({
    mutationFn: (data: { id: number; form: EditPanelForm }) =>
      api.put(`/panels/${data.id}`, data.form),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['panels'] });
      setEditingPanel(null);
      toast.success('Panel updated');
    },
    onError: (err: any) => {
      toast.error(err?.response?.data?.error || 'Failed to update panel');
    },
  });

  const relinkPanelMutation = useMutation({
    mutationFn: (data: { id: number; link_token: string }) =>
      api.post(`/panels/${data.id}/relink`, { link_token: data.link_token }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['panels'] });
      setRelinkTarget(null);
      setRelinkToken('');
      toast.success('Panel relinked with a fresh token');
    },
    onError: (err: any) => {
      toast.error(err?.response?.data?.error || 'Failed to relink panel');
    },
  });

  const deletePanelMutation = useMutation({
    mutationFn: (id: number) => api.delete(`/panels/${id}`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['panels'] });
      setUnlinkTarget(null);
      toast.success('Panel unlinked');
    },
    onError: () => toast.error('Failed to unlink panel'),
  });

  const testPanelMutation = useMutation({
    mutationFn: (id: number) => api.post(`/panels/${id}/test`),
    onSuccess: (res) => {
      queryClient.invalidateQueries({ queryKey: ['panels'] });
      const data = res.data;
      if (data.status === 'online') {
        toast.success(`Connection OK${data.latency_ms != null ? ` (${data.latency_ms}ms)` : ''}`);
      } else {
        toast.error(`Connection failed: ${data.last_error || 'Unknown error'}`);
      }
    },
    onError: () => toast.error('Test request failed'),
  });

  const restorePanelMutation = useMutation({
    mutationFn: (data: { id: number; file: File }) => {
      const formData = new FormData();
      formData.append('file', data.file);
      return api.post(`/panels/${data.id}/restore`, formData);
    },
    onSuccess: () => {
      closeRestore();
      toast.success('Backup uploaded. The node is restarting.');
    },
    onError: (err: any) => {
      toast.error(err?.response?.data?.error || 'Restore failed');
    },
  });

  const openEdit = (panel: LinkedPanel) => {
    setEditingPanel(panel);
    setEditFormData({ name: panel.name, enable: panel.enable });
  };

  const closeRestore = () => {
    setRestoreTarget(null);
    setRestoreFile(null);
    setRestoreConfirmName('');
  };

  const downloadBackup = async (panel: LinkedPanel) => {
    setBackupBusyId(panel.id);
    try {
      const res = await api.get(`/panels/${panel.id}/backup`, { responseType: 'blob' });
      const url = window.URL.createObjectURL(new Blob([res.data]));
      const link = document.createElement('a');
      const stamp = new Date().toISOString().slice(0, 16).replace(/[-:T]/g, '');
      link.href = url;
      link.setAttribute('download', `${panel.name.replace(/[^\w.-]+/g, '_')}-${stamp}.db`);
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.URL.revokeObjectURL(url);
    } catch {
      toast.error(`Could not fetch a backup from "${panel.name}"`);
    } finally {
      setBackupBusyId(null);
    }
  };

  const pickRestoreFile = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.currentTarget.value = '';
    if (!file) return;
    const lowered = file.name.toLowerCase();
    if (!ALLOWED_RESTORE_EXTENSIONS.some((ext) => lowered.endsWith(ext))) {
      toast.error('Unsupported backup format');
      return;
    }
    if (file.size > MAX_RESTORE_FILE_BYTES) {
      toast.error('Backup file is too large (max 50 MB)');
      return;
    }
    setRestoreFile(file);
  };

  const localClientCount = inbounds.reduce(
    (sum, ib) => sum + (ib.settings?.clients?.length || 0),
    0
  );

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-white">Panels</h1>
        <div className="flex items-center gap-2">
          <Button
            onClick={() => {
              setAddFormData(EMPTY_ADD_FORM);
              setShowAddForm(true);
            }}
          >
            <Plus size={16} />
            <span className="ml-1.5">Add Panel</span>
          </Button>
        </div>
      </div>

      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        className="bg-white/[0.02] border border-white/[0.06] rounded-2xl p-5 space-y-3"
      >
        <div className="flex items-start justify-between gap-3">
          <div className="flex items-center gap-3 min-w-0">
            <div className="p-2.5 rounded-xl bg-primary/10">
              <Shield size={22} className="text-primary" />
            </div>
            <div className="min-w-0">
              <h3 className="font-semibold text-white">Master</h3>
              <p className="text-xs text-gray-500">Local panel instance</p>
            </div>
          </div>
          <div className="flex items-center gap-1.5 shrink-0">
            <div className="w-2 h-2 rounded-full bg-emerald-400" />
            <span className="text-xs font-medium text-emerald-400">Local</span>
          </div>
        </div>

        <div className="grid grid-cols-2 gap-2 text-xs">
          <div className="bg-white/[0.03] rounded-lg px-3 py-2">
            <span className="text-gray-500">Inbounds</span>
            <p className="text-gray-300 font-medium">{inbounds.length}</p>
          </div>
          <div className="bg-white/[0.03] rounded-lg px-3 py-2">
            <span className="text-gray-500">Clients</span>
            <p className="text-gray-300 font-medium">{localClientCount}</p>
          </div>
        </div>
      </motion.div>

      {panelsLoading ? (
        <div className="text-center text-gray-500 py-12">Loading...</div>
      ) : panels.length === 0 ? (
        <div className="text-center text-gray-500 py-12">
          <Server size={48} className="mx-auto mb-4 opacity-30" />
          <p>No child panels linked</p>
          <p className="text-sm mt-1">Link a remote panel to enable multi-panel federation</p>
        </div>
      ) : (
        <div className="grid gap-4 grid-cols-1 md:grid-cols-2 xl:grid-cols-3">
          <AnimatePresence mode="popLayout">
            {panels.map((panel, i) => {
              const st = STATUS_ICON[panel.status] || STATUS_ICON.unknown;
              const StatusIcon = st.icon;
              return (
                <motion.div
                  key={panel.id}
                  initial={{ opacity: 0, y: 20 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0, scale: 0.95 }}
                  transition={{ delay: i * 0.05 }}
                  className="bg-white/[0.02] border border-white/[0.06] rounded-2xl p-5 space-y-4"
                >
                  <div className="flex items-start justify-between">
                    <div className="flex items-center gap-3 min-w-0">
                      <div
                        className={`p-2 rounded-xl ${panel.enable ? 'bg-primary/10' : 'bg-gray-800'}`}
                      >
                        <Server
                          size={20}
                          className={panel.enable ? 'text-primary' : 'text-gray-600'}
                        />
                      </div>
                      <div className="min-w-0">
                        <h3 className="font-semibold text-white truncate">{panel.name}</h3>
                        <p className="text-xs text-gray-500 truncate" title={panel.url}>
                          {panel.url}
                        </p>
                      </div>
                    </div>
                    <div className="flex items-center gap-1.5 shrink-0">
                      <StatusIcon size={16} className={st.color} />
                      <span className={`text-xs font-medium ${st.color}`}>{st.label}</span>
                      {panel.app_version && (
                        <span
                          className="ml-2 font-mono text-[10px] text-gray-500"
                          title="Release this node reports running. A node left behind in a wave is the one thing this shows that nothing else does."
                        >
                          v{panel.app_version}
                        </span>
                      )}
                    </div>
                  </div>

                  <div className="grid grid-cols-2 gap-2 text-xs">
                    <div className="bg-white/[0.03] rounded-lg px-3 py-2 flex items-center gap-2">
                      <Clock size={13} className="text-gray-500 shrink-0" />
                      <div className="min-w-0">
                        <span className="text-gray-500">Last Poll</span>
                        <p className="text-gray-300 truncate">{formatLastPoll(panel.last_poll)}</p>
                      </div>
                    </div>
                    <div className="bg-white/[0.03] rounded-lg px-3 py-2 flex items-center gap-2">
                      <Layers size={13} className="text-gray-500 shrink-0" />
                      <div className="min-w-0">
                        <span className="text-gray-500">Created</span>
                        <p className="text-gray-300 truncate">{formatDateTime(panel.created_at)}</p>
                      </div>
                    </div>
                  </div>

                  {panel.status === 'stale' && (
                    <div className="text-xs text-amber-400/90 bg-amber-500/10 rounded-lg px-3 py-1.5">
                      Nothing is polling this panel. Subscriptions and the bot are being served from
                      the copy taken {formatLastPoll(panel.last_poll)} — check the cron host.
                    </div>
                  )}

                  {(panel.reality_failures?.count ?? 0) > 0 && (
                    <div className="text-xs text-amber-400/90 bg-amber-500/10 rounded-lg px-3 py-1.5">
                      {panel.reality_failures?.count} REALITY handshakes were refused on this node
                      in the last hour. A few mean scanners or a stale client; a steady stream with
                      no one connecting means the inbound&apos;s decoy address cannot serve as a
                      REALITY target — try www.google.com and check that the SNI matches
                      PROXY_DOMAIN.
                    </div>
                  )}

                  {!panel.enable && (
                    <div className="text-xs text-yellow-500/80 bg-yellow-500/10 rounded-lg px-3 py-1.5">
                      Disabled
                    </div>
                  )}

                  {panel.last_error && panel.status === 'offline' && (
                    <div
                      className="text-xs text-red-400/80 bg-red-500/10 rounded-lg px-3 py-1.5 truncate"
                      title={panel.last_error}
                    >
                      {panel.last_error}
                    </div>
                  )}

                  <div className="flex flex-wrap gap-2 pt-1">
                    <Button
                      variant="secondary"
                      size="sm"
                      onClick={() => testPanelMutation.mutate(panel.id)}
                      disabled={testPanelMutation.isPending}
                    >
                      <Zap size={14} />
                      <span className="ml-1">Test</span>
                    </Button>
                    <Button
                      variant="secondary"
                      size="sm"
                      onClick={() => downloadBackup(panel)}
                      isLoading={backupBusyId === panel.id}
                    >
                      <Download size={14} />
                      <span className="ml-1">Backup</span>
                    </Button>
                    <Button
                      variant="secondary"
                      size="sm"
                      onClick={() => {
                        setRestoreFile(null);
                        setRestoreConfirmName('');
                        setRestoreTarget(panel);
                      }}
                    >
                      <Upload size={14} />
                      <span className="ml-1">Restore</span>
                    </Button>
                    <Button variant="secondary" size="sm" onClick={() => openEdit(panel)}>
                      <Pencil size={14} />
                      <span className="ml-1">Edit</span>
                    </Button>
                    <Button
                      variant="secondary"
                      size="sm"
                      onClick={() => {
                        setRelinkToken('');
                        setRelinkTarget(panel);
                      }}
                    >
                      <Link2 size={14} />
                      <span className="ml-1">Relink</span>
                    </Button>
                    <Button variant="danger" size="sm" onClick={() => setUnlinkTarget(panel)}>
                      <Trash2 size={14} />
                      <span className="ml-1">Unlink</span>
                    </Button>
                  </div>
                </motion.div>
              );
            })}
          </AnimatePresence>
        </div>
      )}

      <Modal isOpen={showAddForm} onClose={() => setShowAddForm(false)} title="Add Panel">
        <div className="space-y-4">
          <Input
            label="Name"
            placeholder="e.g. Frankfurt Node"
            value={addFormData.name}
            onChange={(e) => setAddFormData({ ...addFormData, name: e.target.value })}
          />
          <Input
            label="Link Token"
            type="password"
            placeholder="Paste the token from the child panel"
            value={addFormData.link_token}
            onChange={(e) => setAddFormData({ ...addFormData, link_token: e.target.value })}
          />
          <div className="flex justify-end gap-2 pt-2">
            <Button variant="secondary" onClick={() => setShowAddForm(false)}>
              Cancel
            </Button>
            <Button
              onClick={() => addPanelMutation.mutate(addFormData)}
              disabled={addPanelMutation.isPending}
            >
              {addPanelMutation.isPending ? 'Linking...' : 'Add'}
            </Button>
          </div>
        </div>
      </Modal>

      <Modal
        isOpen={!!relinkTarget}
        onClose={() => setRelinkTarget(null)}
        title={`Relink ${relinkTarget?.name || ''}`}
      >
        <div className="space-y-4">
          <p className="text-sm leading-relaxed text-gray-400">
            Issue a fresh token on the node itself — System → Link → Revoke access &amp; issue token
            — then paste it here. Tariffs stay on this panel; only the token and the address change.
          </p>
          <Input
            label="Link Token"
            type="password"
            placeholder="Paste the new token from the node panel"
            value={relinkToken}
            onChange={(e) => setRelinkToken(e.target.value)}
          />
          <div className="flex justify-end gap-2 pt-2">
            <Button variant="secondary" onClick={() => setRelinkTarget(null)}>
              Cancel
            </Button>
            <Button
              onClick={() =>
                relinkTarget &&
                relinkPanelMutation.mutate({ id: relinkTarget.id, link_token: relinkToken })
              }
              disabled={!relinkToken.trim() || relinkPanelMutation.isPending}
            >
              {relinkPanelMutation.isPending ? 'Relinking...' : 'Relink'}
            </Button>
          </div>
        </div>
      </Modal>

      <Modal isOpen={!!editingPanel} onClose={() => setEditingPanel(null)} title="Edit Panel">
        <div className="space-y-4">
          <Input
            label="Name"
            value={editFormData.name}
            onChange={(e) => setEditFormData({ ...editFormData, name: e.target.value })}
          />
          <Switch
            label="Enabled"
            checked={editFormData.enable}
            onChange={(e) => setEditFormData({ ...editFormData, enable: e.target.checked })}
          />
          <div className="flex justify-end gap-2 pt-2">
            <Button variant="secondary" onClick={() => setEditingPanel(null)}>
              Cancel
            </Button>
            <Button
              onClick={() =>
                editingPanel &&
                updatePanelMutation.mutate({ id: editingPanel.id, form: editFormData })
              }
              disabled={updatePanelMutation.isPending}
            >
              {updatePanelMutation.isPending ? 'Saving...' : 'Update'}
            </Button>
          </div>
        </div>
      </Modal>

      <Modal isOpen={!!restoreTarget} onClose={closeRestore} title="Restore panel database">
        <div className="space-y-4">
          <div className="text-sm text-red-300/90 bg-red-500/10 border border-red-500/20 rounded-xl px-4 py-3">
            This replaces the entire database of{' '}
            <span className="font-semibold text-red-200">{restoreTarget?.name}</span> — its
            inbounds, its keys and its clients — and restarts the node. It cannot be undone from
            here.
          </div>

          <div>
            <label className="block text-sm text-gray-400 mb-1.5">Backup file</label>
            <div className="relative">
              <input
                type="file"
                accept=".db,.sqlite,.sqlite3"
                className="absolute inset-0 opacity-0 cursor-pointer z-10"
                onChange={pickRestoreFile}
              />
              <div className="h-11 flex items-center px-4 rounded-xl bg-white/[0.04] border border-white/[0.08] text-sm text-gray-300 truncate">
                {restoreFile ? restoreFile.name : 'Choose a .db file…'}
              </div>
            </div>
          </div>

          <Input
            label={`Type "${restoreTarget?.name ?? ''}" to confirm`}
            placeholder={restoreTarget?.name}
            value={restoreConfirmName}
            onChange={(e) => setRestoreConfirmName(e.target.value)}
          />

          <div className="flex justify-end gap-2 pt-2">
            <Button variant="secondary" onClick={closeRestore}>
              Cancel
            </Button>
            <Button
              variant="danger"
              onClick={() =>
                restoreTarget &&
                restoreFile &&
                restorePanelMutation.mutate({ id: restoreTarget.id, file: restoreFile })
              }
              disabled={
                !restoreFile ||
                restoreConfirmName.trim() !== restoreTarget?.name ||
                restorePanelMutation.isPending
              }
            >
              {restorePanelMutation.isPending ? 'Restoring...' : 'Restore'}
            </Button>
          </div>
        </div>
      </Modal>

      <ConfirmationModal
        isOpen={!!unlinkTarget}
        onClose={() => setUnlinkTarget(null)}
        onConfirm={() => unlinkTarget && deletePanelMutation.mutate(unlinkTarget.id)}
        title="Unlink Panel"
        description={`Remove panel "${unlinkTarget?.name}"? The child panel will no longer be managed by this master. Existing users on the child are not affected.`}
        confirmText="Unlink"
        isLoading={deletePanelMutation.isPending}
      />
    </div>
  );
}
