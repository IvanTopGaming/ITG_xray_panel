import { QRCodeSVG } from 'qrcode.react';

export default function QrPanel({ value, size = 180 }: { value: string; size?: number }) {
  return (
    <div className="inline-block rounded-xl bg-white p-2">
      <QRCodeSVG value={value} size={size} level="M" includeMargin />
    </div>
  );
}
