import type { ReactNode } from 'react';
import { daysLeft, formatDate } from '@/lib/format';
import { MONTHS, t, type Lang } from '@/lib/i18n';
import type { SubInfo } from '@/lib/types';

function Box({ label, value, note }: { label: string; value: ReactNode; note: string }) {
  return (
    <div className="flex-1 rounded-2xl border border-white/[0.06] bg-white/[0.04] px-[18px] py-4">
      <div className="text-xs text-muted">{label}</div>
      <div className="mt-[5px] text-[22px] font-bold text-white">{value}</div>
      <div className="mt-[3px] text-xs text-muted">{note || ' '}</div>
    </div>
  );
}

export default function Summary({ data, lang }: { data: SubInfo; lang: Lang }) {
  const expiry = data.expiry_at;
  const until = expiry > 0 ? formatDate(expiry, lang, MONTHS[lang]) : t('never', lang);
  const days = expiry > 0 ? daysLeft(expiry) : 0;
  const note =
    expiry <= 0 ? '' : days <= 0 ? t('expired', lang) : t('days_left', lang, { n: days });

  return (
    <div className="mb-4 flex gap-3.5">
      <Box label={t('valid_until', lang)} value={until} note={note} />
      {data.devices !== null && (
        <Box
          label={t('devices', lang)}
          value={
            data.devices.limit > 0 ? (
              <>
                {data.devices.count}{' '}
                <span className="text-sm text-muted">/ {data.devices.limit}</span>
              </>
            ) : (
              data.devices.count
            )
          }
          note={t('connected', lang)}
        />
      )}
    </div>
  );
}
