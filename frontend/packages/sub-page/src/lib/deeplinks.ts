export type Platform = 'ios' | 'android' | 'desktop';

export interface SubApp {
  id: string;
  label: string;
  scheme: (subUrl: string) => string;
  install: Partial<Record<Platform, string>>;
}

export function detectPlatform(): Platform {
  const ua = navigator.userAgent.toLowerCase();
  if (/iphone|ipad|ipod/.test(ua)) return 'ios';
  if (/macintosh/.test(ua) && navigator.maxTouchPoints > 1) return 'ios';
  if (/android/.test(ua)) return 'android';
  return 'desktop';
}

export const SUB_APPS: readonly SubApp[] = [
  {
    id: 'happ',
    label: 'Happ',
    scheme: (subUrl) => `happ://add/${subUrl}`,
    install: {
      ios: 'https://apps.apple.com/us/app/happ-proxy-utility/id6504287215',
      android: 'https://play.google.com/store/apps/details?id=com.happproxy',
    },
  },
  {
    id: 'v2raytun',
    label: 'v2RayTun',
    scheme: (subUrl) => `v2raytun://import/${subUrl}`,
    install: {
      ios: 'https://apps.apple.com/us/app/v2raytun/id6476628951',
      android: 'https://play.google.com/store/apps/details?id=com.v2raytun.android',
    },
  },
  {
    id: 'hiddify',
    label: 'Hiddify',
    scheme: (subUrl) => `hiddify://import/${subUrl}`,
    install: {
      ios: 'https://apps.apple.com/us/app/hiddify-proxy-vpn/id6596777532',
      android: 'https://play.google.com/store/apps/details?id=app.hiddify.com',
    },
  },
];

export function buildImportUrl(app: SubApp, subUrl: string): string {
  return app.scheme(subUrl);
}
