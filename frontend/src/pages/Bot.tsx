import { useState } from 'react';
import { motion, LayoutGroup } from 'framer-motion';
import { TariffsTab } from '@/components/bot/TariffsTab';
import { TextsTab } from '@/components/bot/TextsTab';
import { UsersTab } from '@/components/bot/UsersTab';
import { GrantsTab } from '@/components/bot/GrantsTab';
import { PaymentsTab } from '../components/bot/PaymentsTab';
import { SettingsTab } from '../components/bot/SettingsTab';

type BotTab = 'tariffs' | 'users' | 'grants' | 'texts' | 'payments' | 'settings';

const TABS: { key: BotTab; label: string }[] = [
  { key: 'tariffs', label: 'Tariffs' },
  { key: 'users', label: 'Users' },
  { key: 'grants', label: 'Granted' },
  { key: 'texts', label: 'Texts' },
  { key: 'payments', label: 'Payments' },
  { key: 'settings', label: 'Settings' },
];

export default function Bot() {
  const [activeTab, setActiveTab] = useState<BotTab>('tariffs');

  return (
    <div className="flex flex-col gap-4 p-4 md:p-6">
      <h1 className="text-2xl font-semibold tracking-tight">Bot</h1>

      <LayoutGroup id="bot-tabs">
        <div className="flex flex-wrap gap-1 rounded-2xl border border-white/[0.05] bg-white/[0.04] p-1">
          {TABS.map((tab) => {
            const isActive = activeTab === tab.key;
            return (
              <button
                key={tab.key}
                onClick={() => setActiveTab(tab.key)}
                className="relative px-4 py-2 text-sm font-medium transition-colors"
              >
                {isActive && (
                  <motion.div
                    layoutId="bot-tab-pill"
                    className="absolute inset-0 rounded-xl border border-white/[0.1] bg-gradient-to-br from-primary/25 to-violet-600/20 shadow-[0_0_12px_rgba(208,188,255,0.12)]"
                    transition={{ type: 'spring', stiffness: 500, damping: 35 }}
                  />
                )}
                <span className="relative">{tab.label}</span>
              </button>
            );
          })}
        </div>
      </LayoutGroup>

      <div className="mt-2">
        {activeTab === 'tariffs' && <TariffsTab />}
        {activeTab === 'texts' && <TextsTab />}
        {activeTab === 'users' && <UsersTab />}
        {activeTab === 'grants' && <GrantsTab />}
        {activeTab === 'payments' && <PaymentsTab />}
        {activeTab === 'settings' && <SettingsTab />}
      </div>
    </div>
  );
}
