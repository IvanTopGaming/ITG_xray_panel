import { useState } from 'react';
import { HeroCards } from '../components/monitoring/HeroCards';
import { RangeSelector } from '../components/monitoring/RangeSelector';
import { UnifiedChart } from '../components/monitoring/UnifiedChart';
import { TopTalkers } from '../components/monitoring/TopTalkers';
import { ContainersTable } from '../components/monitoring/ContainersTable';
import { ProcessesTable } from '../components/monitoring/ProcessesTable';
export default function Monitoring() {
  const [range, setRange] = useState('live');

  return (
    <div className="p-6 space-y-4">
      <div className="flex items-center justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-xl font-semibold text-white/90">Monitoring</h1>
          <p className="text-white/50 text-sm">Host resources & network</p>
        </div>
        <RangeSelector value={range} onChange={setRange} />
      </div>
      <HeroCards />
      <div className="flex flex-col lg:flex-row gap-4">
        <div className="lg:flex-[2] min-w-0">
          <UnifiedChart range={range} />
        </div>
        <div className="lg:flex-1 min-w-0">
          <TopTalkers />
        </div>
      </div>
      <div className="flex flex-col lg:flex-row gap-4">
        <div className="lg:flex-[3] min-w-0">
          <ContainersTable />
        </div>
        <div className="lg:flex-[2] min-w-0">
          <ProcessesTable />
        </div>
      </div>
    </div>
  );
}
