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
    status_expired: 'Истекла',
    status_blocked: 'Заблокирована',
    status_traffic_exhausted: 'Трафик исчерпан',
    status_not_configured: 'Не настроена',
    unknown: 'неизвестно',
    load_not_found: 'Ссылка не найдена. Проверь её или запроси актуальную ссылку у бота.',
    load_unavailable:
      'Сервис временно недоступен. Повтори попытку — состояние подписки сейчас неизвестно.',
    load_invalid_response:
      'Не удалось прочитать данные подписки. Повтори попытку или обратись к администратору.',
    hero_title: 'Подключить за один тап',
    copy: 'Скопировать ссылку',
    copied: 'Скопировано',
    show_qr: 'Показать QR',
    hide_qr: 'Скрыть QR',
    scan_hint: 'Отсканируйте телефоном',
    apps_desktop:
      'Кнопки работают на телефоне. С компьютера отсканируйте QR или скопируйте ссылку.',
    qr_alt: 'QR-код со ссылкой на подписку',
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
    format_help:
      'Если приложение не поддерживает формат или протокол, попробуй другой формат либо запроси совместимую конфигурацию у администратора.',
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
    status_expired: 'Expired',
    status_blocked: 'Blocked',
    status_traffic_exhausted: 'Traffic exhausted',
    status_not_configured: 'Not configured',
    unknown: 'unknown',
    load_not_found: 'Link not found. Check it or ask the bot for your current subscription link.',
    load_unavailable:
      'Service temporarily unavailable. Retry shortly; subscription status is currently unknown.',
    load_invalid_response: 'Could not read subscription data. Retry or contact your administrator.',
    hero_title: 'Connect in one tap',
    copy: 'Copy link',
    copied: 'Copied to clipboard',
    show_qr: 'Show QR',
    hide_qr: 'Hide QR',
    scan_hint: 'Scan with your phone',
    apps_desktop: 'The buttons work on a phone. On a computer, scan the QR or copy the link.',
    qr_alt: 'QR code containing the subscription link',
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
    format_help:
      'If your app does not support a format or protocol, try another format or ask your administrator for a compatible configuration.',
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
