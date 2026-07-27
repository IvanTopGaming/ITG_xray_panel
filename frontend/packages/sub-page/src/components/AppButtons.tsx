import { useEffect, useRef, useState } from 'react';
import { SUB_APPS, buildImportUrl, detectPlatform, type SubApp } from '@/lib/deeplinks';
import { t, type Lang } from '@/lib/i18n';

const FALLBACK_DELAY_MS = 1200;

export default function AppButtons({ subUrl, lang }: { subUrl: string; lang: Lang }) {
  const [missing, setMissing] = useState<string | null>(null);
  const pending = useRef<(() => void) | null>(null);
  const platform = detectPlatform();
  const isDesktop = platform === 'desktop';

  useEffect(
    () => () => {
      pending.current?.();
      pending.current = null;
    },
    []
  );

  function open(app: SubApp) {
    pending.current?.();
    setMissing(null);

    let hidden = false;
    const onHide = () => {
      hidden = true;
    };
    const detach = () => {
      document.removeEventListener('visibilitychange', onHide);
      window.removeEventListener('blur', onHide);
      window.removeEventListener('pagehide', onHide);
    };

    document.addEventListener('visibilitychange', onHide);
    window.addEventListener('blur', onHide);
    window.addEventListener('pagehide', onHide);

    const timer = window.setTimeout(() => {
      pending.current = null;
      detach();
      if (!hidden && document.visibilityState === 'visible') setMissing(app.id);
    }, FALLBACK_DELAY_MS);

    pending.current = () => {
      window.clearTimeout(timer);
      detach();
    };

    window.location.href = buildImportUrl(app, subUrl);
  }

  const missingApp = missing ? SUB_APPS.find((app) => app.id === missing) : undefined;
  const installUrl = missingApp?.install[platform];

  return (
    <div>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
        {SUB_APPS.map((app) => (
          <button
            key={app.id}
            type="button"
            onClick={() => open(app)}
            disabled={isDesktop}
            className="flex items-center justify-center gap-2 rounded-xl border border-white/[0.08] bg-white/[0.055] px-3 py-2.5 text-sm font-medium transition hover:bg-white/[0.09] disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-white/[0.055]"
          >
            {app.label}
          </button>
        ))}
      </div>
      {isDesktop && <p className="mt-2.5 text-[11px] text-muted">{t('apps_desktop', lang)}</p>}
      {missingApp && (
        <p role="status" aria-live="polite" className="mt-3 text-xs text-muted">
          {t('not_installed', lang, { app: missingApp.label })}{' '}
          {installUrl && (
            <a
              className="text-primary underline"
              href={installUrl}
              target="_blank"
              rel="noreferrer"
            >
              {t('install', lang)}
            </a>
          )}
        </p>
      )}
    </div>
  );
}
