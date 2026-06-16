import { useSnapshot } from './useSnapshot';
import { pickValue, fmtBytes, fmtRate, fmtPct } from './format';

function Card({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="bg-white/[0.04] rounded-2xl border border-white/[0.05] p-4 flex flex-col gap-1 min-w-0 flex-1">
      <span className="text-xs font-medium text-white/40 uppercase tracking-wider">{label}</span>
      <div className="text-2xl font-semibold text-white/90 mt-1">{children}</div>
    </div>
  );
}

export function HeroCards() {
  const { data: snap } = useSnapshot();

  const cpu = fmtPct(pickValue(snap, 'cpu_host', 'host'));
  const ram = fmtBytes(pickValue(snap, 'ram_host', 'host'));
  const netRx = fmtRate(pickValue(snap, 'net_host_rx', 'host'));
  const netTx = fmtRate(pickValue(snap, 'net_host_tx', 'host'));
  const diskR = fmtRate(pickValue(snap, 'disk_io', 'disk', 'read'));
  const diskW = fmtRate(pickValue(snap, 'disk_io', 'disk', 'write'));

  return (
    <div className="flex flex-wrap gap-3">
      <Card label="CPU">{cpu}</Card>
      <Card label="RAM">{ram}</Card>
      <Card label="Network">
        <span className="text-lg">↓ {netRx}</span>
        <span className="text-lg ml-2">↑ {netTx}</span>
      </Card>
      <Card label="Disk IO">
        <span className="text-lg">↓ {diskR}</span>
        <span className="text-lg ml-2">↑ {diskW}</span>
      </Card>
    </div>
  );
}
