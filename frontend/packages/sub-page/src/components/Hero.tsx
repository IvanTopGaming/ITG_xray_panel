import { useCallback, useEffect, useRef, useState } from 'react';
import { t, type Lang } from '@/lib/i18n';
import type { SubInfo } from '@/lib/types';
import AppButtons from '@/components/AppButtons';
import QrPanel from '@/components/QrPanel';

async function writeToClipboard(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {}

  const area = document.createElement('textarea');
  area.value = text;
  area.setAttribute('readonly', '');
  area.style.position = 'fixed';
  area.style.opacity = '0';
  document.body.appendChild(area);
  area.select();
  let ok = false;
  try {
    ok = document.execCommand('copy');
  } catch {}
  document.body.removeChild(area);
  return ok;
}

export default function Hero({ data, lang }: { data: SubInfo; lang: Lang }) {
  const [copied, setCopied] = useState(false);
  const [qrOpen, setQrOpen] = useState(false);
  const timer = useRef<number | undefined>(undefined);

  useEffect(() => () => window.clearTimeout(timer.current), []);

  const copy = useCallback(async () => {
    if (!(await writeToClipboard(data.sub_url))) return;
    setCopied(true);
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setCopied(false), 1600);
  }, [data.sub_url]);

  return (
    <div className="mb-4 rounded-[20px] border border-primary/20 bg-gradient-to-br from-[rgba(79,55,139,0.4)] to-[rgba(124,77,255,0.12)] p-5 backdrop-blur-xl">
      <h2 className="mb-4 text-[13px] font-medium uppercase tracking-[1px] text-[#d8c9ff]">
        {t('hero_title', lang)}
      </h2>
      <div className="md:flex md:gap-6">
        <div className="hidden shrink-0 text-center md:block">
          <QrPanel value={data.sub_url} lang={lang} />
          <p className="mt-2 text-[11px] text-muted">{t('scan_hint', lang)}</p>
        </div>
        <div className="min-w-0 flex-1">
          <AppButtons subUrl={data.sub_url} lang={lang} />
          <div className="mb-3.5 mt-3.5 break-all rounded-xl border border-primary/[0.15] bg-black/[0.35] px-4 py-3.5 font-mono text-[13px] text-[#d8c9ff]">
            {data.sub_url}
          </div>
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              onClick={copy}
              className="flex flex-1 items-center justify-center gap-2 rounded-xl bg-gradient-to-br from-primary to-[#7c4dff] px-5 py-3 text-sm font-medium text-[#1a1228] shadow-[0_0_18px_rgba(208,188,255,0.25)] transition hover:-translate-y-px hover:shadow-[0_0_26px_rgba(208,188,255,0.45)]"
            >
              ⧉ {t('copy', lang)}
            </button>
            <button
              type="button"
              onClick={() => setQrOpen((open) => !open)}
              aria-expanded={qrOpen}
              className="rounded-xl border border-white/[0.08] bg-white/[0.055] px-5 py-3 text-sm font-medium transition hover:bg-white/[0.09] md:hidden"
            >
              {t(qrOpen ? 'hide_qr' : 'show_qr', lang)}
            </button>
          </div>
          {qrOpen && (
            <div className="mt-3.5 text-center md:hidden">
              <QrPanel value={data.sub_url} size={220} lang={lang} />
              <p className="mt-2 text-[11px] text-muted">{t('scan_hint', lang)}</p>
            </div>
          )}
          <p className="mt-3.5 text-xs text-muted">{t('hint', lang)}</p>
        </div>
      </div>
      <div
        role="status"
        aria-live="polite"
        className={`pointer-events-none fixed bottom-6 left-1/2 -translate-x-1/2 rounded-full border border-white/[0.08] bg-[#1a1a26] px-4 py-2.5 text-[13px] transition-opacity duration-200 ${copied ? 'opacity-100' : 'opacity-0'}`}
      >
        {t('copied', lang)}
      </div>
    </div>
  );
}
