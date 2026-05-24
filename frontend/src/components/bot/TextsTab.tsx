import { useMemo, useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { toast } from 'react-toastify';
import { motion, AnimatePresence } from 'framer-motion';
import { listBotTexts, listBotTextKeys, updateBotText, resetBotText } from '@/lib/bot';
import type { BotTextKeyMeta } from '@/lib/types';

interface RowState {
  ru: string;
  en: string;
  ruDirty: boolean;
  enDirty: boolean;
}

function groupKeysByNamespace(keys: BotTextKeyMeta[]) {
  const groups: Record<string, BotTextKeyMeta[]> = {};
  for (const k of keys) {
    const ns = k.key.split('.')[0];
    if (!groups[ns]) groups[ns] = [];
    groups[ns].push(k);
  }
  return groups;
}

export function TextsTab() {
  const queryClient = useQueryClient();
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Record<string, RowState>>({});

  const keysQuery = useQuery({
    queryKey: ['bot', 'texts', 'keys'],
    queryFn: listBotTextKeys,
  });
  const textsQuery = useQuery({
    queryKey: ['bot', 'texts'],
    queryFn: listBotTexts,
  });

  const groups = useMemo(
    () => (keysQuery.data ? groupKeysByNamespace(keysQuery.data) : {}),
    [keysQuery.data]
  );

  const selectedMeta = useMemo(
    () => keysQuery.data?.find((k) => k.key === selectedKey) ?? null,
    [keysQuery.data, selectedKey]
  );

  const lookupRow = (key: string, lang: 'ru' | 'en'): string => {
    if (drafts[key] && drafts[key][lang] !== undefined) {
      return drafts[key][lang];
    }
    const row = textsQuery.data?.find((r) => r.key === key && r.lang === lang);
    if (row) return row.text;
    const meta = keysQuery.data?.find((k) => k.key === key);
    return meta ? (lang === 'ru' ? meta.default_ru : meta.default_en) : '';
  };

  const setDraft = (key: string, lang: 'ru' | 'en', value: string) => {
    setDrafts((prev) => {
      const cur = prev[key] || { ru: '', en: '', ruDirty: false, enDirty: false };
      return {
        ...prev,
        [key]: {
          ...cur,
          [lang]: value,
          [`${lang}Dirty`]: true,
        } as RowState,
      };
    });
  };

  const saveMutation = useMutation({
    mutationFn: ({ key, lang, text }: { key: string; lang: 'ru' | 'en'; text: string }) =>
      updateBotText(key, lang, text),
    onSuccess: (row) => {
      toast.success('Saved');
      setDrafts((prev) => {
        const cur = prev[row.key] || {
          ru: '',
          en: '',
          ruDirty: false,
          enDirty: false,
        };
        return {
          ...prev,
          [row.key]: { ...cur, [`${row.lang}Dirty`]: false } as RowState,
        };
      });
      queryClient.invalidateQueries({ queryKey: ['bot', 'texts'] });
    },
    onError: () => toast.error('Save failed'),
  });

  const resetMutation = useMutation({
    mutationFn: ({ key, lang }: { key: string; lang: 'ru' | 'en' }) => resetBotText(key, lang),
    onSuccess: (_, vars) => {
      toast.success('Reset to default');
      setDrafts((prev) => {
        const cur = prev[vars.key];
        if (!cur) return prev;
        return {
          ...prev,
          [vars.key]: { ...cur, [`${vars.lang}Dirty`]: false } as RowState,
        };
      });
      queryClient.invalidateQueries({ queryKey: ['bot', 'texts'] });
    },
    onError: () => toast.error('Reset failed'),
  });

  if (keysQuery.isLoading || textsQuery.isLoading) {
    return <p className="text-sm text-white/60">Loading…</p>;
  }
  if (keysQuery.error || textsQuery.error) {
    return <p className="text-sm text-rose-400">Failed to load texts.</p>;
  }
  if ((keysQuery.data?.length ?? 0) === 0) {
    return (
      <div className="rounded-xl border border-white/[0.05] bg-white/[0.02] p-8 text-center text-sm text-white/60">
        No keys defined. Populate bot_texts_defaults.yaml and re-run the backend migration.
      </div>
    );
  }

  return (
    <div className="grid grid-cols-12 gap-4">
      {/* Left: tree */}
      <div className="col-span-4 max-h-[70vh] overflow-y-auto rounded-2xl border border-white/[0.05] bg-white/[0.02] p-3 scrollbar-thin scrollbar-track-transparent scrollbar-thumb-white/10 hover:scrollbar-thumb-white/20 pr-2">
        {Object.entries(groups)
          .sort(([a], [b]) => a.localeCompare(b))
          .map(([ns, keys]) => (
            <div key={ns} className="mb-3">
              <p className="px-2 py-1 text-xs uppercase text-white/40">{ns}</p>
              {keys.map((k) => {
                const isActive = k.key === selectedKey;
                const isDirty = drafts[k.key]?.ruDirty || drafts[k.key]?.enDirty;
                return (
                  <button
                    key={k.key}
                    onClick={() => setSelectedKey(k.key)}
                    className={`relative flex w-full items-center justify-between rounded-xl px-3 py-2 text-left text-xs transition-colors ${
                      isActive
                        ? 'bg-primary/20 text-primary-100 font-medium shadow-[0_0_12px_rgba(208,188,255,0.08)]'
                        : 'text-white/60 hover:bg-white/[0.06] hover:text-white/90'
                    }`}
                  >
                    <span className="font-mono">{k.key}</span>
                    {isDirty && <span className="ml-2 h-2 w-2 rounded-full bg-amber-400" />}
                  </button>
                );
              })}
            </div>
          ))}
      </div>

      {/* Right: editor */}
      <div className="col-span-8">
        <AnimatePresence mode="wait">
          {selectedMeta ? (
            <motion.div
              key={selectedMeta.key}
              initial={{ opacity: 0, y: 6 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -6 }}
              transition={{ duration: 0.18 }}
              className="flex flex-col gap-4 rounded-2xl border border-white/[0.05] bg-white/[0.02] p-6 shadow-sm"
            >
              <div>
                <p className="font-mono text-sm text-white/90">{selectedMeta.key}</p>
                <p className="text-xs text-white/60">
                  {selectedMeta.description || 'No description'}
                </p>
                {selectedMeta.variables.length > 0 && (
                  <p className="mt-1 text-xs text-white/50">
                    Variables:{' '}
                    {selectedMeta.variables.map((v) => (
                      <code key={v} className="mx-0.5 rounded bg-white/[0.06] px-1 text-[11px]">
                        {`{${v}}`}
                      </code>
                    ))}
                  </p>
                )}
              </div>

              {(['ru', 'en'] as const).map((lang) => {
                const value = lookupRow(selectedMeta.key, lang);
                const dirty = drafts[selectedMeta.key]?.[`${lang}Dirty`];
                return (
                  <div key={lang} className="flex flex-col gap-1">
                    <div className="flex items-center justify-between">
                      <span className="text-xs uppercase text-white/50">{lang}</span>
                      <div className="flex gap-2">
                        <button
                          type="button"
                          disabled={!dirty}
                          onClick={() =>
                            saveMutation.mutate({
                              key: selectedMeta.key,
                              lang,
                              text: value,
                            })
                          }
                          className="rounded-lg bg-primary/20 px-3 py-1 text-xs font-medium text-primary-100 transition-colors hover:bg-primary/30 focus:outline-none focus:ring-2 focus:ring-primary/50 disabled:opacity-40 disabled:cursor-not-allowed"
                        >
                          Save
                        </button>
                        <button
                          type="button"
                          onClick={() =>
                            resetMutation.mutate({
                              key: selectedMeta.key,
                              lang,
                            })
                          }
                          className="rounded-lg bg-white/[0.05] px-3 py-1 text-xs font-medium text-white/80 transition-colors hover:bg-white/[0.1] hover:text-white focus:outline-none focus:ring-2 focus:ring-white/20"
                        >
                          Reset
                        </button>
                      </div>
                    </div>
                    <textarea
                      value={value}
                      onChange={(e) => setDraft(selectedMeta.key, lang, e.target.value)}
                      rows={4}
                      className="w-full rounded-xl border border-white/[0.08] bg-black/40 px-3 py-2 font-mono text-sm text-white transition-colors focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary/50 placeholder-white/30 resize-y"
                    />
                  </div>
                );
              })}
            </motion.div>
          ) : (
            <div className="rounded-xl border border-white/[0.05] bg-white/[0.02] p-8 text-center text-sm text-white/60">
              Select a key on the left to edit.
            </div>
          )}
        </AnimatePresence>
      </div>
    </div>
  );
}
