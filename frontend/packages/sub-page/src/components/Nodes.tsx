import { formatBytes, formatDate } from '@/lib/format';
import { MONTHS, t, type Lang } from '@/lib/i18n';
import type { SubInfo, SubNode } from '@/lib/types';

function NodeRow({ node, lang }: { node: SubNode; lang: Lang }) {
  const pct = node.limit > 0 ? Math.min(100, (node.used * 100) / node.limit) : 0;
  const warn = pct >= 90;
  const until =
    node.expiry > 0 ? `${t('until', lang)} ${formatDate(node.expiry, lang, MONTHS[lang])}` : '';

  return (
    <div
      className={`border-b border-white/[0.05] py-4 first:pt-0 last:border-none last:pb-0 ${node.enabled ? '' : 'opacity-[0.42]'}`}
    >
      <div className="mb-3 flex items-center gap-2.5">
        <span
          className={`h-2.5 w-2.5 shrink-0 rounded-full ${node.online ? 'bg-ok shadow-[0_0_8px_#7ee787]' : 'bg-[#6f6781]'}`}
        />
        <span className="flex-1 truncate text-[15px] font-medium">{node.name}</span>
        <span className="shrink-0 rounded-md border border-primary/[0.18] bg-primary/[0.12] px-2 py-0.5 text-[11px] text-[#cabfe0]">
          {node.tag}
        </span>
      </div>
      {node.limit <= 0 ? (
        <div className="flex justify-between gap-3 text-xs text-muted">
          <span>
            ∞ {t('unlimited', lang)}{' '}
            <b className="font-medium text-[#cabfe0]">{formatBytes(node.used)}</b>
          </span>
          <span className="shrink-0">{until}</span>
        </div>
      ) : (
        <>
          <div className="h-[7px] overflow-hidden rounded-full bg-white/[0.08]">
            <div
              className={`h-full rounded-full ${warn ? 'bg-gradient-to-r from-[#f59e0b] to-warn' : 'bg-gradient-to-r from-[#7c4dff] to-primary'}`}
              style={{ width: `${pct.toFixed(0)}%` }}
            />
          </div>
          <div className="mt-2 flex justify-between gap-3 text-xs text-muted">
            <span className={warn ? 'text-warn' : undefined}>
              {formatBytes(node.used)} {t('of_gb', lang)} {formatBytes(node.limit)}
              {warn ? ` · ${t('almost', lang)}` : ''}
            </span>
            <span className="shrink-0">{until}</span>
          </div>
        </>
      )}
    </div>
  );
}

export default function Nodes({ data, lang }: { data: SubInfo; lang: Lang }) {
  return (
    <div className="mb-4 rounded-[20px] border border-white/[0.06] bg-white/[0.04] p-5 backdrop-blur-xl">
      <h2 className="mb-4 text-[13px] font-medium uppercase tracking-[1px] text-muted">
        {t('nodes', lang)} · {data.nodes.length}
      </h2>
      {data.nodes.length === 0 ? (
        <p className="text-sm text-muted">{t('no_nodes', lang)}</p>
      ) : (
        data.nodes.map((node, i) => (
          <NodeRow key={`${node.name}-${node.tag}-${i}`} node={node} lang={lang} />
        ))
      )}
    </div>
  );
}
