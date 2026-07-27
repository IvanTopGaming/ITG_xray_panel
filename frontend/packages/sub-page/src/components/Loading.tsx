import { t, type Lang } from '@/lib/i18n';

export default function Loading({ lang }: { lang: Lang }) {
  return (
    <div className="mx-auto max-w-3xl px-4 pb-16 pt-8">
      <div className="mb-7 flex items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <span className="h-[42px] w-[42px] animate-pulse rounded-[13px] border border-white/[0.06] bg-white/[0.06]" />
          <span className="h-4 w-40 animate-pulse rounded-md bg-white/[0.06]" />
        </div>
        <span className="h-7 w-24 animate-pulse rounded-full bg-white/[0.06]" />
      </div>
      <div className="mb-4 h-[168px] animate-pulse rounded-[20px] border border-white/[0.06] bg-white/[0.04]" />
      <div className="mb-4 flex gap-3.5">
        <div className="h-[104px] flex-1 animate-pulse rounded-2xl border border-white/[0.06] bg-white/[0.04]" />
        <div className="h-[104px] flex-1 animate-pulse rounded-2xl border border-white/[0.06] bg-white/[0.04]" />
      </div>
      <div className="mb-4 h-[200px] animate-pulse rounded-[20px] border border-white/[0.06] bg-white/[0.04]" />
      <p role="status" aria-live="polite" className="mt-[22px] text-center text-xs text-muted">
        {t('loading', lang)}
      </p>
    </div>
  );
}
