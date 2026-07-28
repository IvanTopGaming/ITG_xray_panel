import { useState, useRef, useEffect, useMemo } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import api from '@ui/lib/api';
import { Button } from '@ui/components/ui/Button';
import { Input } from '@ui/components/ui/Input';
import { Select } from '@ui/components/ui/Select';
import {
  Terminal,
  Download,
  Upload,
  Shield,
  Power,
  Server,
  Database,
  KeyRound,
  Search,
  FileJson,
  Copy,
  Check,
  Info,
  Heart,
  Github,
  Radar,
  Link2,
} from 'lucide-react';
import { toast } from 'react-toastify';
import { motion, AnimatePresence } from 'framer-motion';
import { ConfirmationModal } from '@ui/components/ui/ConfirmationModal';
import { formatDateForPicker, formatDateTime, formatTime } from '@ui/lib/datetime';
import { Modal } from '@ui/components/ui/Modal';
import { useLogStore } from '@ui/stores/logStore';
import { useAuthStore } from '@ui/stores/authStore';
import { useVersionStatus } from '@ui/hooks/useVersionStatus';
import { hasLocalXray, isWorker } from '@ui/lib/panelRole';
import { FederationConfig } from '@ui/lib/types';

const MAX_RESTORE_FILE_BYTES = 50 * 1024 * 1024;
const ALLOWED_RESTORE_EXTENSIONS = ['.db', '.sqlite', '.sqlite3'];
type SettingsTab = 'security' | 'core' | 'federation' | 'maintenance' | 'about';

const SETTINGS_TABS: { id: SettingsTab; label: string }[] = [
  { id: 'security', label: 'Security' },
  ...(hasLocalXray ? [{ id: 'core' as SettingsTab, label: 'Core' }] : []),
  ...(isWorker ? [{ id: 'federation' as SettingsTab, label: 'Link' }] : []),
  { id: 'maintenance', label: 'Maintenance' },
  { id: 'about', label: 'About' },
];

const GITHUB_URL = 'https://github.com/IvanTopGaming/ITG_xray_panel';

export default function System() {
  const { logs, isStreaming, toggleStream } = useLogStore();
  const { logout } = useAuthStore();
  const queryClient = useQueryClient();
  const { services, hasUpdates } = useVersionStatus();

  const [confirmRestart, setConfirmRestart] = useState(false);
  const [confirmPassword, setConfirmPassword] = useState(false);
  const [confirmGeoUpdate, setConfirmGeoUpdate] = useState(false);
  const [configModal, setConfigModal] = useState(false);
  const [configContent, setConfigContent] = useState('');
  const [isCopied, setIsCopied] = useState(false);
  const [searchTerm, setSearchTerm] = useState('');
  const logEndRef = useRef<HTMLDivElement>(null);
  const [password, setPassword] = useState('');
  const [passError, setPassError] = useState('');
  const [xrayLogLevel, setXrayLogLevel] = useState('info');
  const [geoipUrl, setGeoipUrl] = useState('');
  const [geositeUrl, setGeositeUrl] = useState('');
  const [activeTab, setActiveTab] = useState<SettingsTab>(hasLocalXray ? 'core' : 'security');
  const [confirmRevoke, setConfirmRevoke] = useState(false);
  const [isTokenCopied, setIsTokenCopied] = useState(false);

  const { data: federation } = useQuery<FederationConfig>({
    queryKey: ['federation-config'],
    queryFn: async () => (await api.get('/federation/config')).data,
    enabled: isWorker,
  });

  const linkTokenMutation = useMutation({
    mutationFn: async () => (await api.post('/federation/link-token')).data,
    onSuccess: (data: { revoked: boolean }) => {
      setConfirmRevoke(false);
      setIsTokenCopied(false);
      queryClient.invalidateQueries({ queryKey: ['federation-config'] });
      toast.success(
        data.revoked
          ? 'Access revoked. Paste the new token into the master panel to link again.'
          : 'Link token issued'
      );
    },
    onError: () => toast.error('Could not issue a link token'),
  });

  const handleCopyLinkToken = async () => {
    if (!federation?.link_token) return;
    try {
      await navigator.clipboard.writeText(federation.link_token);
      setIsTokenCopied(true);
      setTimeout(() => setIsTokenCopied(false), 2000);
    } catch {
      toast.error('Could not copy to clipboard');
    }
  };

  const { data: systemSettings, isFetching: isSettingsFetching } = useQuery({
    queryKey: ['system-settings'],
    queryFn: async () => (await api.get('/system/settings')).data,
    enabled: hasLocalXray,
  });

  useEffect(() => {
    if (isStreaming && !searchTerm) {
      logEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    }
  }, [logs, isStreaming]);

  useEffect(() => {
    if (!systemSettings) return;
    setXrayLogLevel(systemSettings.xrayLogLevel || 'info');
    setGeoipUrl(systemSettings.geoipUrl || '');
    setGeositeUrl(systemSettings.geositeUrl || '');
  }, [systemSettings]);

  const restartMutation = useMutation({
    mutationFn: () => api.post('/restart'),
    onSuccess: () => {
      toast.success('System Restarting...');
      setConfirmRestart(false);
    },
  });

  const updateGeoMutation = useMutation({
    mutationFn: () => api.post('/system/update-geo'),
    onSuccess: () => toast.success('Databases updated'),
  });

  const saveSystemSettingsMutation = useMutation({
    mutationFn: () =>
      api.put('/system/settings', {
        xrayLogLevel,
        geoipUrl: geoipUrl.trim(),
        geositeUrl: geositeUrl.trim(),
      }),
    onSuccess: () => {
      toast.success('System settings saved');
      queryClient.invalidateQueries({ queryKey: ['system-settings'] });
    },
    onError: (err: any) => {
      toast.error(err.response?.data?.error || 'Failed to save settings');
    },
  });

  const passwordMutation = useMutation({
    mutationFn: () => api.put('/admin/password', { new_password: password }),
    onSuccess: () => {
      toast.success('Password changed. Please log in again.');
      logout();
    },
    onError: (err: any) => {
      toast.error(err.response?.data?.error || 'Failed to change password');
      setConfirmPassword(false);
    },
  });

  const fetchConfig = async () => {
    try {
      const res = await api.get('/config');
      setConfigContent(JSON.stringify(res.data, null, 2));
      setConfigModal(true);
    } catch (e: any) {
      toast.error(e.response?.data?.error || 'Failed to fetch configuration');
    }
  };

  const copyConfig = async () => {
    try {
      await navigator.clipboard.writeText(configContent);
      setIsCopied(true);
      toast.success('Config copied to clipboard');
      setTimeout(() => setIsCopied(false), 2000);
    } catch {
      toast.error('Clipboard access denied');
    }
  };

  const handlePasswordChange = () => {
    setPassError('');
    if (password.length < 8) {
      setPassError('Must be at least 8 characters');
      return;
    }
    if (!/^[\x20-\x7E]*$/.test(password)) {
      setPassError('Only printable ASCII characters allowed');
      return;
    }
    if (!/[A-Z]/.test(password)) {
      setPassError('Must contain at least one uppercase letter');
      return;
    }
    if (!/[a-z]/.test(password)) {
      setPassError('Must contain at least one lowercase letter');
      return;
    }
    if (!/[0-9]/.test(password)) {
      setPassError('Must contain at least one digit');
      return;
    }
    setConfirmPassword(true);
  };

  const handleBackup = async () => {
    try {
      const res = await api.get('/backup', { responseType: 'blob' });
      const url = window.URL.createObjectURL(new Blob([res.data]));
      const link = document.createElement('a');
      link.href = url;
      link.setAttribute('download', `backup-${formatDateForPicker(Date.now())}.db`);
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.URL.revokeObjectURL(url);
    } catch {
      toast.error('Backup failed');
    }
  };

  const handleRestore = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const loweredName = file.name.toLowerCase();
    const hasAllowedExtension = ALLOWED_RESTORE_EXTENSIONS.some((item) =>
      loweredName.endsWith(item)
    );
    if (!hasAllowedExtension) {
      toast.error('Unsupported backup format');
      e.currentTarget.value = '';
      return;
    }
    if (file.size > MAX_RESTORE_FILE_BYTES) {
      toast.error('Backup file is too large (max 50 MB)');
      e.currentTarget.value = '';
      return;
    }
    const formData = new FormData();
    formData.append('file', file);
    try {
      await api.post('/restore', formData);
      toast.success('Restored. Restarting...');
      setTimeout(() => window.location.reload(), 3000);
    } catch (err: any) {
      toast.error(err.response?.data?.error || 'Restore failed');
    } finally {
      e.currentTarget.value = '';
    }
  };

  const filteredLogs = useMemo(
    () => logs.filter((entry) => entry.text.toLowerCase().includes(searchTerm.toLowerCase())),
    [logs, searchTerm]
  );

  const handleSaveSystemSettings = () => {
    try {
      const geoipParsed = new URL(geoipUrl.trim());
      const geositeParsed = new URL(geositeUrl.trim());
      if (!['http:', 'https:'].includes(geoipParsed.protocol)) {
        toast.error('GeoIP URL must start with http:// or https://');
        return;
      }
      if (!['http:', 'https:'].includes(geositeParsed.protocol)) {
        toast.error('GeoSite URL must start with http:// or https://');
        return;
      }
    } catch {
      toast.error('Enter valid GeoIP/GeoSite URLs');
      return;
    }
    saveSystemSettingsMutation.mutate();
  };

  return (
    <div
      className={`grid grid-cols-1 gap-8 pb-10 items-start ${
        hasLocalXray ? 'lg:grid-cols-3' : 'max-w-xl mx-auto w-full'
      }`}
    >
      {hasLocalXray && (
        <div className="lg:col-span-2 relative group self-start order-2 lg:order-1">
          <div className="absolute -inset-0.5 bg-gradient-to-r from-green-500/20 to-emerald-500/20 rounded-2xl blur opacity-75 group-hover:opacity-100 transition duration-1000"></div>
          <div className="relative bg-[#0a0a0a] rounded-2xl flex flex-col h-[400px] md:h-[600px] border border-white/10 shadow-2xl overflow-hidden">
            <div className="flex flex-wrap items-center justify-between px-4 py-3 bg-[#151515] border-b border-white/5 gap-3">
              <div className="flex items-center gap-4">
                <div className="flex gap-2 shrink-0">
                  <div className="w-3 h-3 rounded-full bg-red-500/80"></div>
                  <div className="w-3 h-3 rounded-full bg-yellow-500/80"></div>
                  <div className="w-3 h-3 rounded-full bg-green-500/80"></div>
                </div>
                <div className="text-xs font-mono text-gray-500 flex items-center gap-2 whitespace-nowrap">
                  <Terminal size={12} /> root@xray-panel:~
                </div>
              </div>

              <div className="relative w-full sm:w-auto">
                <input
                  type="text"
                  name="system_fake_username"
                  autoComplete="username"
                  className="hidden"
                  tabIndex={-1}
                />
                <input
                  type="password"
                  name="system_fake_password"
                  autoComplete="current-password"
                  className="hidden"
                  tabIndex={-1}
                />
                <Search
                  className="absolute left-2.5 top-1/2 -translate-y-1/2 text-gray-600"
                  size={12}
                />
                <input
                  type="text"
                  name="system_log_search"
                  placeholder="Search logs..."
                  value={searchTerm}
                  onChange={(e) => setSearchTerm(e.target.value)}
                  autoComplete="off"
                  autoCorrect="off"
                  autoCapitalize="off"
                  spellCheck={false}
                  className="w-full sm:w-48 bg-black/40 border border-white/10 rounded-lg pl-8 pr-3 py-1 text-[10px] font-mono text-gray-300 focus:outline-none focus:border-white/20 transition-all placeholder:text-gray-700"
                />
              </div>
            </div>

            <div className="flex-1 overflow-y-auto p-4 space-y-1 font-mono text-xs custom-scrollbar bg-[#0a0a0a]">
              {filteredLogs.map((entry, i) => (
                <div key={i} className="break-all leading-relaxed">
                  <span className="text-gray-600 mr-3 select-none">[{formatTime(entry.ts)}]</span>
                  <span
                    className={
                      entry.text.toLowerCase().includes('error')
                        ? 'text-red-400'
                        : entry.text.toLowerCase().includes('warning')
                          ? 'text-yellow-400'
                          : 'text-green-400/90'
                    }
                  >
                    {entry.text}
                  </span>
                </div>
              ))}
              {filteredLogs.length === 0 && searchTerm && (
                <div className="text-gray-600 italic text-center py-4">
                  No logs matching &quot;{searchTerm}&quot;
                </div>
              )}
              {isStreaming && !searchTerm && (
                <div className="animate-pulse text-green-500 mt-2">_</div>
              )}
              <div ref={logEndRef} />
            </div>

            <div className="p-4 bg-[#151515] border-t border-white/5">
              <Button
                className={`w-full font-mono text-xs h-10 ${isStreaming ? 'bg-red-500/10 text-red-500 hover:bg-red-500/20' : 'bg-green-500/10 text-green-500 hover:bg-green-500/20'}`}
                onClick={toggleStream}
              >
                {isStreaming ? 'STOP PROCESS' : 'INITIALIZE LOG STREAM'}
              </Button>
            </div>
          </div>
        </div>
      )}

      <div className="space-y-4 lg:sticky lg:top-6 self-start order-1 lg:order-2">
        <div className="flex gap-1 bg-white/[0.04] p-1 rounded-2xl border border-white/[0.05]">
          {SETTINGS_TABS.map((tab) => (
            <button
              key={tab.id}
              type="button"
              onClick={() => setActiveTab(tab.id)}
              className="relative flex-1 py-2 text-xs font-bold uppercase tracking-wider rounded-xl transition-colors"
              style={{ color: activeTab === tab.id ? '#fff' : 'rgba(156,163,175,1)' }}
            >
              {activeTab === tab.id && (
                <motion.div
                  layoutId="systemTabPill"
                  className="absolute inset-0 bg-gradient-to-br from-primary/25 to-violet-600/20 rounded-xl border border-white/[0.1] shadow-[0_0_12px_rgba(208,188,255,0.12)]"
                  transition={{ type: 'spring', stiffness: 500, damping: 35 }}
                />
              )}
              <span className="relative z-10">{tab.label}</span>
              {hasUpdates && tab.id === 'about' && (
                <span
                  title="Update available"
                  className="absolute right-2 top-1.5 z-20 h-2 w-2 rounded-full bg-primary shadow-[0_0_8px_rgba(208,188,255,0.8)]"
                />
              )}
            </button>
          ))}
        </div>

        <AnimatePresence mode="wait">
          <motion.div
            key={activeTab}
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -4 }}
            transition={{ duration: 0.16, ease: 'easeOut' }}
          >
            {activeTab === 'security' && (
              <SettingsCard title="Security" icon={<Shield size={18} className="text-primary" />}>
                <div className="space-y-4">
                  <Input
                    type="password"
                    placeholder="New Password (min 8 chars)"
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    className="bg-black/20"
                    error={passError}
                    autoComplete="new-password"
                  />
                  <Button
                    className="w-full h-11"
                    onClick={handlePasswordChange}
                    disabled={!password}
                  >
                    <KeyRound size={16} className="mr-2" /> Change Password
                  </Button>
                </div>
              </SettingsCard>
            )}

            {hasLocalXray && activeTab === 'core' && (
              <SettingsCard
                title="Core Settings"
                icon={<Database size={18} className="text-secondary" />}
              >
                <div className="space-y-4">
                  <Select
                    label="Xray Log Level"
                    value={xrayLogLevel}
                    onChange={(e) => setXrayLogLevel(e.target.value)}
                    options={[
                      { value: 'debug', label: 'debug' },
                      { value: 'info', label: 'info' },
                      { value: 'warning', label: 'warning' },
                      { value: 'error', label: 'error' },
                      { value: 'none', label: 'none' },
                    ]}
                    disabled={isSettingsFetching}
                  />
                  <Input
                    label="GeoIP URL"
                    value={geoipUrl}
                    onChange={(e) => setGeoipUrl(e.target.value)}
                    placeholder="https://.../geoip.dat"
                    disabled={isSettingsFetching}
                    autoComplete="off"
                  />
                  <Input
                    label="GeoSite URL"
                    value={geositeUrl}
                    onChange={(e) => setGeositeUrl(e.target.value)}
                    placeholder="https://.../geosite.dat"
                    disabled={isSettingsFetching}
                    autoComplete="off"
                  />
                  <Button
                    className="w-full h-11"
                    onClick={handleSaveSystemSettings}
                    isLoading={saveSystemSettingsMutation.isPending}
                    disabled={isSettingsFetching}
                  >
                    Save Core Settings
                  </Button>
                </div>
              </SettingsCard>
            )}

            {isWorker && activeTab === 'federation' && (
              <SettingsCard title="Master Link" icon={<Link2 size={18} className="text-primary" />}>
                <div className="space-y-4">
                  <div className="flex items-center justify-between rounded-xl border border-white/5 bg-black/20 px-4 py-3">
                    <span className="text-xs font-bold uppercase tracking-wider text-gray-500">
                      Status
                    </span>
                    <span
                      className={`text-sm font-semibold ${federation?.is_linked ? 'text-emerald-400' : 'text-gray-400'}`}
                    >
                      {federation?.is_linked ? 'Linked' : 'Not linked'}
                    </span>
                  </div>

                  {federation?.master_url && (
                    <div className="space-y-1 rounded-xl border border-white/5 bg-black/20 px-4 py-3">
                      <div className="text-xs font-bold uppercase tracking-wider text-gray-500">
                        {federation.is_linked ? 'Master panel' : 'Last master panel'}
                      </div>
                      <div className="text-sm text-gray-200">
                        {federation.master_name || 'Unnamed'}
                      </div>
                      <div className="break-all font-mono text-xs text-gray-500">
                        {federation.master_url}
                      </div>
                      {federation.linked_at && (
                        <div className="text-xs text-gray-500">
                          Linked {formatDateTime(federation.linked_at)}
                        </div>
                      )}
                    </div>
                  )}

                  {federation?.link_token && (
                    <div className="space-y-2">
                      <div className="text-xs font-bold uppercase tracking-wider text-gray-500">
                        Link token
                      </div>
                      <div className="max-h-28 overflow-y-auto break-all rounded-xl border border-white/5 bg-black/30 p-3 font-mono text-xs text-gray-300">
                        {federation.link_token}
                      </div>
                      <Button
                        variant="secondary"
                        className={`w-full h-10 ${isTokenCopied ? 'text-green-400 border-green-400/30' : ''}`}
                        onClick={handleCopyLinkToken}
                      >
                        {isTokenCopied ? (
                          <Check size={16} className="mr-2" />
                        ) : (
                          <Copy size={16} className="mr-2" />
                        )}
                        {isTokenCopied ? 'Copied' : 'Copy token'}
                      </Button>
                      <p className="text-xs leading-relaxed text-gray-500">
                        Paste it in the master panel — Panels → Add Panel for a new node, or Relink
                        on an existing one. It carries this panel&apos;s address and works once.
                      </p>
                    </div>
                  )}

                  <Button
                    className="h-11 w-full"
                    variant={federation?.is_linked ? 'danger' : 'primary'}
                    onClick={() =>
                      federation?.is_linked ? setConfirmRevoke(true) : linkTokenMutation.mutate()
                    }
                    isLoading={linkTokenMutation.isPending}
                  >
                    {federation?.is_linked ? 'Revoke access & issue token' : 'Issue link token'}
                  </Button>
                </div>
              </SettingsCard>
            )}

            {activeTab === 'maintenance' && (
              <SettingsCard
                title="Maintenance"
                icon={<Server size={18} className="text-secondary" />}
              >
                <div className="space-y-3">
                  {hasLocalXray ? (
                    <>
                      <Button
                        variant="secondary"
                        className="w-full justify-start h-12 bg-white/5 hover:bg-white/10"
                        onClick={handleBackup}
                      >
                        <div className="p-2 bg-black/20 rounded-lg mr-3">
                          <Download size={16} />
                        </div>
                        Backup Database
                      </Button>
                      <div className="relative">
                        <input
                          type="file"
                          accept=".db,.sqlite,.sqlite3"
                          className="absolute inset-0 opacity-0 cursor-pointer z-10"
                          onChange={handleRestore}
                        />
                        <Button
                          variant="secondary"
                          className="w-full justify-start h-12 bg-white/5 hover:bg-white/10"
                        >
                          <div className="p-2 bg-black/20 rounded-lg mr-3">
                            <Upload size={16} />
                          </div>
                          Restore Database
                        </Button>
                      </div>
                    </>
                  ) : (
                    <div className="rounded-xl border border-white/[0.08] bg-white/[0.03] p-4 text-sm text-gray-400">
                      <p className="text-gray-300 font-medium mb-1">
                        Database backup lives elsewhere
                      </p>
                      <p>
                        This panel keeps its data in Postgres. Back the data tier up with the{' '}
                        <span className="text-gray-200 font-mono">pg-backup</span> container from{' '}
                        <span className="text-gray-200 font-mono">docker-compose.postgres.yml</span>
                        . A node&apos;s own database is backed up from its card on the Panels page.
                      </p>
                    </div>
                  )}
                  {hasLocalXray && (
                    <>
                      <Button
                        variant="secondary"
                        className="w-full justify-start h-12 bg-white/5 hover:bg-white/10"
                        onClick={() => setConfirmGeoUpdate(true)}
                        isLoading={updateGeoMutation.isPending}
                      >
                        <div className="p-2 bg-black/20 rounded-lg mr-3">
                          <Database
                            size={16}
                            className={updateGeoMutation.isPending ? 'animate-spin' : ''}
                          />
                        </div>
                        Update GeoIP
                      </Button>
                      <Button
                        variant="secondary"
                        className="w-full justify-start h-12 bg-white/5 hover:bg-white/10"
                        onClick={fetchConfig}
                      >
                        <div className="p-2 bg-black/20 rounded-lg mr-3">
                          <FileJson size={16} />
                        </div>
                        View Configuration
                      </Button>
                      <div className="h-px bg-white/10 my-2" />

                      <Button
                        variant="danger"
                        className="w-full justify-start h-12"
                        onClick={() => setConfirmRestart(true)}
                      >
                        <div className="p-2 bg-black/20 rounded-lg mr-3">
                          <Power size={16} />
                        </div>
                        Restart Core
                      </Button>
                    </>
                  )}
                </div>
              </SettingsCard>
            )}

            {activeTab === 'about' && (
              <SettingsCard title="About" icon={<Info size={18} className="text-primary" />}>
                <div className="flex flex-col items-center text-center gap-5 py-2">
                  <motion.div
                    whileHover={{ rotate: 180, scale: 1.08 }}
                    transition={{ duration: 0.5 }}
                    className="p-3 bg-primary/10 rounded-2xl text-primary"
                  >
                    <Radar size={32} />
                  </motion.div>

                  <div>
                    <div className="text-lg font-bold text-gray-100 tracking-tight">
                      ITG Xray Panel
                    </div>
                    <div className="mt-1 text-xs font-mono text-gray-500">
                      v{__APP_VERSIONS__[__FRONTEND_VERSION_KEY__]}
                    </div>
                  </div>

                  <div className="w-full grid grid-cols-2 gap-2 text-[11px] font-mono">
                    {services.map((s) => (
                      <VersionPill
                        key={s.key}
                        label={s.label}
                        value={s.current}
                        latest={
                          s.current !== null ? (s.updateAvailable ? s.latest : null) : s.latest
                        }
                        isLocal={s.isLocal}
                      />
                    ))}
                  </div>

                  <div className="text-sm text-gray-400">
                    Developed by <span className="text-gray-200 font-semibold">ITG</span>
                  </div>

                  <div className="inline-flex items-center gap-1.5 text-xs text-gray-500">
                    Made with
                    <motion.span
                      animate={{ scale: [1, 1.18, 1] }}
                      transition={{ duration: 1.4, repeat: Infinity, ease: 'easeInOut' }}
                      className="inline-flex"
                    >
                      <Heart size={13} className="text-error fill-error" />
                    </motion.span>
                    from ITG
                  </div>

                  <a
                    href={GITHUB_URL}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="w-full inline-flex items-center justify-center gap-2 h-11 rounded-xl bg-white/[0.04] hover:bg-white/[0.08] border border-white/10 hover:border-white/20 text-sm font-semibold text-gray-200 transition-colors"
                  >
                    <Github size={16} />
                    View on GitHub
                  </a>
                </div>
              </SettingsCard>
            )}
          </motion.div>
        </AnimatePresence>

        {hasLocalXray && (
          <>
            <ConfirmationModal
              isOpen={confirmRestart}
              onClose={() => setConfirmRestart(false)}
              onConfirm={() => restartMutation.mutate()}
              title="Restart Xray Core"
              description="Are you sure you want to restart the Xray Core service? All current connections will be dropped."
              confirmText="Restart"
            />

            <ConfirmationModal
              isOpen={confirmGeoUpdate}
              onClose={() => setConfirmGeoUpdate(false)}
              onConfirm={() => {
                updateGeoMutation.mutate();
                setConfirmGeoUpdate(false);
              }}
              title="Update GeoIP / GeoSite"
              description="Downloading updated geo databases will restart the Xray core. All active connections will be briefly interrupted."
              confirmText="Update"
              isLoading={updateGeoMutation.isPending}
            />
          </>
        )}

        <ConfirmationModal
          isOpen={confirmRevoke}
          onClose={() => setConfirmRevoke(false)}
          onConfirm={() => linkTokenMutation.mutate()}
          title="Revoke access & issue token"
          description="The master panel loses access to this node immediately: it stops being polled, provisioned and managed. It comes back once you paste the new token into the master panel and relink."
          confirmText="Revoke"
          isLoading={linkTokenMutation.isPending}
        />

        <ConfirmationModal
          isOpen={confirmPassword}
          onClose={() => setConfirmPassword(false)}
          onConfirm={() => passwordMutation.mutate()}
          title="Change Password"
          description="Are you sure you want to change the admin password? You will need to login again."
          confirmText="Change Password"
          isLoading={passwordMutation.isPending}
          confirmVariant="primary"
        />

        {hasLocalXray && (
          <Modal
            isOpen={configModal}
            onClose={() => setConfigModal(false)}
            title="Current Xray Config"
            maxWidth="max-w-4xl"
          >
            <div className="relative">
              <div className="absolute top-2 right-2 z-10">
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={copyConfig}
                  className={isCopied ? 'text-green-400 border-green-400/30' : ''}
                >
                  {isCopied ? (
                    <Check size={16} className="mr-2" />
                  ) : (
                    <Copy size={16} className="mr-2" />
                  )}
                  {isCopied ? 'Copied' : 'Copy'}
                </Button>
              </div>
              <div className="bg-[#0a0a0a] rounded-xl p-4 border border-white/10 overflow-auto max-h-[70vh] custom-scrollbar">
                <pre className="text-xs font-mono text-gray-300 leading-relaxed whitespace-pre-wrap">
                  {configContent}
                </pre>
              </div>
            </div>
          </Modal>
        )}
      </div>
    </div>
  );
}

function SettingsCard({
  title,
  icon,
  children,
}: {
  title: string;
  icon: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div className="bg-[#1e1b24]/80 backdrop-blur-md p-6 rounded-2xl border border-white/5 shadow-xl">
      <h3 className="flex items-center gap-3 font-bold mb-6 text-gray-200 text-lg">
        <div className="p-2 rounded-xl bg-white/5 border border-white/5">{icon}</div>
        {title}
      </h3>
      {children}
    </div>
  );
}

function VersionPill({
  label,
  value,
  latest,
  isLocal,
}: {
  label: string;
  value: string | null;
  latest?: string | null;
  isLocal?: boolean;
}) {
  const isInformational = value === null;
  const isActionable = Boolean(latest) && !isInformational;
  return (
    <div
      className={`flex items-center justify-between gap-3 px-3 py-2 bg-black/20 rounded-lg border ${
        isActionable ? 'col-span-2 border-primary/25' : 'border-white/5'
      }`}
    >
      <span className="shrink-0 text-gray-500 uppercase tracking-wider">
        {label}
        {isLocal && <span className="ml-1.5 normal-case text-primary/70">(this host)</span>}
      </span>
      <span className="flex items-center gap-2">
        {value !== null && <span className="whitespace-nowrap text-gray-200">{value}</span>}
        {latest && (
          <span
            title={isInformational ? `Published version: ${latest}` : `Update available: ${latest}`}
            className={`shrink-0 whitespace-nowrap rounded border px-1.5 py-0.5 text-[9px] font-bold ${
              isActionable
                ? 'border-primary/40 bg-primary/10 text-primary'
                : 'border-white/10 bg-white/5 text-gray-400'
            }`}
          >
            {isInformational ? latest : `↑ ${latest}`}
          </span>
        )}
      </span>
    </div>
  );
}
