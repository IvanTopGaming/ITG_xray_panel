// Centralized date/time formatting for the panel.
//
// The backend stores datetimes in UTC and emits ISO 8601 strings via
// `.isoformat()` — Python's stdlib does NOT append a 'Z' suffix on naive UTC
// datetimes, so `new Date(iso)` would treat them as local time and shift them
// by the browser's offset. `parseUtcIso` defends against that: if the string
// has no timezone marker, it's assumed to be UTC.
//
// The display timezone is loaded from the panel's `/bot/settings` endpoint
// on app start (see `useDisplayConfigSync` in components/DisplayConfigLoader)
// and stored in a module-level variable so synchronous formatters can reach
// it without ceremony. Default until first fetch resolves: Europe/Moscow.

let _timezone = 'Europe/Moscow';

export function setDisplayTimezone(tz: string): void {
  if (tz) _timezone = tz;
}

export function getDisplayTimezone(): string {
  return _timezone;
}

function parseUtcIso(iso: string): Date {
  // ISO with explicit TZ (Z or ±HH:MM) — trust it.
  if (/[zZ]$|[+-]\d{2}:?\d{2}$/.test(iso)) return new Date(iso);
  // Naive — assume UTC.
  return new Date(iso + 'Z');
}

export function parseDate(input: string | number | null | undefined): Date | null {
  return parseInput(input);
}

function parseInput(input: string | number | null | undefined): Date | null {
  if (input === null || input === undefined) return null;
  if (typeof input === 'number') {
    // Treat as Unix epoch ms (consistent with Client.expiry_time, etc.)
    return new Date(input);
  }
  if (!input) return null;
  return parseUtcIso(input);
}

const DATETIME_OPTIONS: Intl.DateTimeFormatOptions = {
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false, // 24h everywhere — no "05:26 PM"
};

const DATE_OPTIONS: Intl.DateTimeFormatOptions = {
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
};

export function formatDateTime(input: string | number | null | undefined, fallback = '—'): string {
  const d = parseInput(input);
  if (!d || isNaN(d.getTime())) return fallback;
  return new Intl.DateTimeFormat(undefined, {
    ...DATETIME_OPTIONS,
    timeZone: _timezone,
  }).format(d);
}

export function formatDate(input: string | number | null | undefined, fallback = '—'): string {
  const d = parseInput(input);
  if (!d || isNaN(d.getTime())) return fallback;
  return new Intl.DateTimeFormat(undefined, {
    ...DATE_OPTIONS,
    timeZone: _timezone,
  }).format(d);
}

// "YYYY-MM-DD" rendered in the configured timezone. Used for HTML
// <input type="date"> defaults and filename suffixes.
export function formatDateForPicker(input: string | number | null | undefined): string {
  const d = parseInput(input);
  if (!d || isNaN(d.getTime())) return '';
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat('en-CA', {
      timeZone: _timezone,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
    })
      .formatToParts(d)
      .map((p) => [p.type, p.value])
  );
  return `${parts.year}-${parts.month}-${parts.day}`;
}

// "HH:MM" 24h, in the configured timezone — for log lines and other
// compact in-line timestamps where seconds and date are irrelevant.
export function formatTime(input: string | number | null | undefined, fallback = '—'): string {
  const d = parseInput(input);
  if (!d || isNaN(d.getTime())) return fallback;
  return new Intl.DateTimeFormat(undefined, {
    timeZone: _timezone,
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(d);
}

// Escape hatch for callers that need custom Intl options (chart tick labels,
// tooltips). Always wired to the configured timezone.
export function formatWith(
  input: string | number | null | undefined,
  options: Intl.DateTimeFormatOptions,
  fallback = '—'
): string {
  const d = parseInput(input);
  if (!d || isNaN(d.getTime())) return fallback;
  return new Intl.DateTimeFormat(undefined, { ...options, timeZone: _timezone }).format(d);
}

// "YYYY-MM-DDTHH:MM" in the configured timezone — what an HTML
// <input type="datetime-local"> expects as its value attribute.
export function formatDateTimeForLocalInput(input: string | number | null | undefined): string {
  const d = parseInput(input);
  if (!d || isNaN(d.getTime())) return '';
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat('en-CA', {
      timeZone: _timezone,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    })
      .formatToParts(d)
      .map((p) => [p.type, p.value])
  );
  let hour = parts.hour;
  if (hour === '24') hour = '00';
  return `${parts.year}-${parts.month}-${parts.day}T${hour}:${parts.minute}`;
}

// Inverse of formatDateTimeForLocalInput: interpret "YYYY-MM-DDTHH:MM" as a
// wall-clock time in the configured timezone, return Unix epoch in ms.
export function epochMsFromLocalDateTimeInput(str: string): number {
  if (!str) return 0;
  const want = `${str}:00`;
  const wantAsUtc = Date.parse(want + 'Z');
  const fmt = new Intl.DateTimeFormat('en-CA', {
    timeZone: _timezone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  });
  const parts = Object.fromEntries(fmt.formatToParts(wantAsUtc).map((p) => [p.type, p.value]));
  let hour = parts.hour;
  if (hour === '24') hour = '00';
  const seenStr = `${parts.year}-${parts.month}-${parts.day}T${hour}:${parts.minute}:${parts.second}`;
  const seenAsUtc = Date.parse(seenStr + 'Z');
  return wantAsUtc + (wantAsUtc - seenAsUtc);
}

// Same as above but returns seconds — Statistics chart endpoints take Unix
// seconds for their range parameters.
export function epochSecFromLocalDateTimeInput(str: string): number {
  return Math.floor(epochMsFromLocalDateTimeInput(str) / 1000);
}
