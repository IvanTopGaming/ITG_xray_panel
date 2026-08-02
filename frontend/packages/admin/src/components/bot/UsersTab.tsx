import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { AnimatePresence, motion } from 'framer-motion';
import { listBotUsers } from '@/lib/bot';
import type { BotUser } from '@ui/lib/types';
import { Search } from 'lucide-react';
import { UserDrawer } from './UserDrawer';
import { formatDate } from '@ui/lib/datetime';

export function UsersTab() {
  const [search, setSearch] = useState('');
  const [selectedTgId, setSelectedTgId] = useState<number | null>(null);

  const usersQuery = useQuery({
    queryKey: ['bot', 'users'],
    queryFn: listBotUsers,
  });

  const filtered = useMemo(() => {
    const data = usersQuery.data || [];
    const q = search.trim().toLowerCase();
    if (!q) return data;
    return data.filter((u: BotUser) => {
      const username = (u.username || '').toLowerCase();
      const tgIdStr = String(u.telegram_id);
      return username.includes(q) || tgIdStr.includes(q);
    });
  }, [usersQuery.data, search]);

  if (usersQuery.isLoading) return <p className="text-sm text-white/60">Loading…</p>;
  if (usersQuery.error) return <p className="text-sm text-rose-400">Failed to load users.</p>;

  return (
    <div className="flex flex-col gap-4">
      <div className="relative">
        <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-white/40" />
        <input
          type="text"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search by Telegram ID or username…"
          className="w-full rounded-xl border border-white/[0.08] bg-white/[0.03] pl-10 pr-4 py-2.5 text-sm text-white placeholder-white/40 transition-colors focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary/50"
        />
      </div>

      {filtered.length === 0 && (
        <div className="rounded-xl border border-white/[0.05] bg-white/[0.02] p-8 text-center text-sm text-white/60">
          {search ? 'No users match the search.' : 'No telegram users yet.'}
        </div>
      )}

      {filtered.length > 0 && (
        <div className="overflow-x-auto rounded-2xl border border-white/[0.05] bg-white/[0.02]">
          <table className="w-full text-sm whitespace-nowrap">
            <thead className="bg-black/40 text-left text-xs uppercase tracking-wider text-white/50">
              <tr>
                <th className="px-4 py-3 font-medium">TG ID</th>
                <th className="px-4 py-3 font-medium">Username</th>
                <th className="px-4 py-3 font-medium">Lang</th>
                <th className="px-4 py-3 font-medium">Trial</th>
                <th className="px-4 py-3 font-medium">Clients</th>
                <th className="px-4 py-3 font-medium">Grants</th>
                <th className="px-4 py-3 font-medium">Last seen</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-white/[0.04]">
              <AnimatePresence>
                {filtered.map((u: BotUser) => (
                  <motion.tr
                    key={u.telegram_id}
                    layout="position"
                    variants={{
                      initial: { opacity: 0, y: 6 },
                      animate: {
                        opacity: 1,
                        y: 0,
                        transition: { duration: 0.25, ease: [0.25, 0.46, 0.45, 0.94] },
                      },
                      exit: {
                        opacity: 0,
                        x: -20,
                        transition: { duration: 0.18, ease: [0.4, 0, 1, 1] },
                      },
                    }}
                    transition={{ layout: { type: 'spring', stiffness: 400, damping: 35 } }}
                    initial="initial"
                    animate="animate"
                    exit="exit"
                    onClick={() => setSelectedTgId(u.telegram_id)}
                    className="cursor-pointer transition-colors hover:bg-white/[0.04]"
                  >
                    <td className="px-4 py-3 font-mono text-white/90">{u.telegram_id}</td>
                    <td className="px-4 py-3 text-white/90">{u.username || '—'}</td>
                    <td className="px-4 py-3 uppercase text-white/60">{u.language}</td>
                    <td className="px-4 py-3 text-white/60">{u.trial_used_at ? '✓' : '—'}</td>
                    <td className="px-4 py-3 text-white/60">{u.clients_count}</td>
                    <td className="px-4 py-3 text-white/60">{u.grants_count}</td>
                    <td className="px-4 py-3 text-white/60">{formatDate(u.last_seen_at)}</td>
                  </motion.tr>
                ))}
              </AnimatePresence>
            </tbody>
          </table>
        </div>
      )}

      <UserDrawer
        open={selectedTgId !== null}
        telegramId={selectedTgId}
        onClose={() => setSelectedTgId(null)}
      />
    </div>
  );
}
