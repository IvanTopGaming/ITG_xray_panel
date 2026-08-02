import { t, type Lang } from '@/lib/i18n';
import type { SubInfo } from '@/lib/types';

const FORMATS: { ua: string; label: string; file: string }[] = [
  { ua: 'v2ray', label: 'v2ray', file: 'config.txt' },
  { ua: 'clash', label: 'Clash', file: 'config.yaml' },
  { ua: 'singbox', label: 'sing-box', file: 'config.json' },
];

export default function Footer({ data, lang }: { data: SubInfo; lang: Lang }) {
  return (
    <div className="mt-[22px] text-center text-xs leading-[1.8] text-[#6f6781]">
      <div>
        {t('download', lang)}:{' '}
        {FORMATS.map((format, i) => (
          <span key={format.ua}>
            {i > 0 ? ' · ' : ''}
            <a
              className="border-b border-dotted border-[#555] text-muted no-underline"
              href={`${data.sub_url}?ua=${format.ua}`}
              download={format.file}
            >
              {format.label}
            </a>
          </span>
        ))}
      </div>
      <div>{t('auto_update', lang, { h: data.update_interval_hours })}</div>
    </div>
  );
}
