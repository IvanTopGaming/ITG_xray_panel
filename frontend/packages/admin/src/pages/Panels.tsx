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
  HelpCircle,
  Zap,
  Shield,
  Layers,
  Clock,
} from 'lucide-react';
import { toast } from 'react-toastify';
import { motion, AnimatePresence } from 'framer-motion';

const STATUS_ICON: Record<string, { icon: typeof Wifi; color: string; label: string }> = {
  online: { icon: Wifi, color: 'text-emerald-400', label: 'Online' },
  offline: { icon: WifiOff, color: 'text-red-400', label: 'Offline' },
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

  const openEdit = (panel: LinkedPanel) => {
    setEditingPanel(panel);
    setEditFormData({ name: panel.name, enable: panel.enable });
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

                  <div className="flex gap-2 pt-1">
                    <Button
                      variant="secondary"
                      size="sm"
                      onClick={() => testPanelMutation.mutate(panel.id)}
                      disabled={testPanelMutation.isPending}
                    >
                      <Zap size={14} />
                      <span className="ml-1">Test</span>
                    </Button>
                    <Button variant="secondary" size="sm" onClick={() => openEdit(panel)}>
                      <Pencil size={14} />
                      <span className="ml-1">Edit</span>
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
