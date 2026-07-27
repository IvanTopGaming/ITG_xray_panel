import { t, type Lang } from '@/lib/i18n';

export default function ErrorState({ lang, onRetry }: { lang: Lang; onRetry: () => void }) {
  return (
    <div className="mx-auto max-w-3xl px-4 pb-16 pt-8">
      <div className="rounded-[20px] border border-white/[0.06] bg-white/[0.04] p-5 text-center backdrop-blur-xl">
        <div className="mx-auto mb-4 flex h-[42px] w-[42px] items-center justify-center rounded-[13px] border border-error/25 bg-error/[0.12] text-xl">
          ⚠
        </div>
        <p className="mb-4 text-[15px] text-[#eae6f0]">{t('load_failed', lang)}</p>
        <button
          type="button"
          onClick={onRetry}
          className="inline-flex items-center justify-center gap-2 rounded-xl bg-gradient-to-br from-primary to-[#7c4dff] px-5 py-3 text-sm font-medium text-[#1a1228] shadow-[0_0_18px_rgba(208,188,255,0.25)] transition hover:shadow-[0_0_26px_rgba(208,188,255,0.45)]"
        >
          ↻ {t('retry', lang)}
        </button>
      </div>
    </div>
  );
}
