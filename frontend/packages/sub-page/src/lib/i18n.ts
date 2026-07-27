export type Lang = 'ru' | 'en';

type Dict = Record<string, string>;

export const MONTHS: Record<Lang, readonly string[]> = {
  ru: [
    'января',
    'февраля',
    'марта',
    'апреля',
    'мая',
    'июня',
    'июля',
    'августа',
    'сентября',
    'октября',
    'ноября',
    'декабря',
  ],
  en: [
    'January',
    'February',
    'March',
    'April',
    'May',
    'June',
    'July',
    'August',
    'September',
    'October',
    'November',
    'December',
  ],
};

const STRINGS: Record<Lang, Dict> = {
  ru: {
    default_brand: 'Подписка',
    status_active: 'Активна',
    status_disabled: 'Отключена',
    hero_title: 'Подключить за один тап',
    copy: 'Скопировать ссылку',
    copied: 'Скопировано',
    show_qr: 'Показать QR',
    hide_qr: 'Скрыть QR',
    scan_hint: 'Отсканируйте телефоном',
    apps_desktop:
      'Кнопки работают на телефоне. С компьютера отсканируйте QR или скопируйте ссылку.',
    not_installed: 'Похоже, {app} не установлен.',
    install: 'Установить',
    hint: 'Вставьте в Happ · v2RayTun · Streisand · Hiddify. Конфиги обновятся сами — при смене серверов переимпортировать ничего не надо.',
    valid_until: 'Действует до',
    days_left: 'осталось {n} дн.',
    expired: 'истекла',
    never: 'бессрочно',
    devices: 'Устройства',
    connected: 'подключено',
    nodes: 'Узлы',
    of_gb: 'из',
    almost: 'почти исчерпан',
    unlimited: 'Безлимит · использовано',
    until: 'до',
    download: 'Скачать конфиг',
    auto_update: 'Подписка обновляется автоматически каждые {h} ч',
    no_nodes: 'Пока нет узлов',
    loading: 'Загружаем данные подписки…',
    load_failed: 'Не удалось загрузить данные подписки',
    retry: 'Повторить',
  },
  en: {
    default_brand: 'Subscription',
    status_active: 'Active',
    status_disabled: 'Disabled',
    hero_title: 'Connect in one tap',
    copy: 'Copy link',
    copied: 'Copied to clipboard',
    show_qr: 'Show QR',
    hide_qr: 'Hide QR',
    scan_hint: 'Scan with your phone',
    apps_desktop: 'The buttons work on a phone. On a computer, scan the QR or copy the link.',
    not_installed: 'Looks like {app} is not installed.',
    install: 'Install',
    hint: 'Paste into Happ · v2RayTun · Streisand · Hiddify. Configs refresh themselves — no re-import when servers change.',
    valid_until: 'Valid until',
    days_left: '{n} days left',
    expired: 'expired',
    never: 'no expiry',
    devices: 'Devices',
    connected: 'connected',
    nodes: 'Nodes',
    of_gb: 'of',
    almost: 'almost exhausted',
    unlimited: 'Unlimited · used',
    until: 'until',
    download: 'Download config',
    auto_update: 'Subscription updates automatically every {h} h',
    no_nodes: 'No nodes yet',
    loading: 'Loading your subscription…',
    load_failed: 'Could not load subscription data',
    retry: 'Retry',
  },
};

export function pickLang(): Lang {
  const query = new URLSearchParams(window.location.search).get('lang');
  const raw = (query || navigator.language || 'en').slice(0, 2).toLowerCase();
  return raw === 'ru' ? 'ru' : 'en';
}

export function t(key: string, lang: Lang, vars?: Record<string, string | number>): string {
  const value = STRINGS[lang][key] ?? STRINGS.en[key] ?? `⟨${key}⟩`;
  if (!vars) return value;
  return Object.entries(vars).reduce((acc, [k, v]) => acc.replace(`{${k}}`, String(v)), value);
}
