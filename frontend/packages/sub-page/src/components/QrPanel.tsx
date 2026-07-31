import { QRCodeSVG } from 'qrcode.react';
import { t, type Lang } from '@/lib/i18n';

export default function QrPanel({
  value,
  size = 180,
  lang,
}: {
  value: string;
  size?: number;
  lang: Lang;
}) {
  return (
    <div className="inline-block rounded-xl bg-white p-2">
      <QRCodeSVG
        value={value}
        size={size}
        level="M"
        includeMargin
        role="img"
        aria-label={t('qr_alt', lang)}
      />
    </div>
  );
}
