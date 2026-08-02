import { t, type Lang } from '@/lib/i18n';
import type { SubInfo } from '@/lib/types';

export default function Header({ data, lang }: { data: SubInfo; lang: Lang }) {
  const active = data.status === 'active';
  const pill = active
    ? 'rounded-full border border-ok/25 bg-ok/[0.12] px-3 py-1.5 text-xs font-medium text-ok'
    : 'rounded-full border border-error/25 bg-error/[0.12] px-3 py-1.5 text-xs font-medium text-error';

  return (
    <div className="mb-7 flex items-center justify-between gap-3">
      <div className="flex min-w-0 items-center gap-3">
        <span className="flex h-[42px] w-[42px] shrink-0 items-center justify-center rounded-[13px] border border-primary/25 bg-gradient-to-br from-primary/[0.18] to-[rgba(124,77,255,0.12)] text-xl shadow-[0_0_22px_rgba(208,188,255,0.18)]">
          🔑
        </span>
        <h1 className="truncate text-[19px] font-bold tracking-[0.3px]">
          {data.brand || t('default_brand', lang)}
        </h1>
      </div>
      <span className={`shrink-0 whitespace-nowrap ${pill}`}>
        ● {t(active ? 'status_active' : 'status_disabled', lang)}
      </span>
    </div>
  );
}
