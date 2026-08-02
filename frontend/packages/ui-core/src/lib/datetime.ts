let _timezone = 'Europe/Moscow';

export function setDisplayTimezone(tz: string): void {
  if (tz) _timezone = tz;
}

export function getDisplayTimezone(): string {
  return _timezone;
}

function parseUtcIso(iso: string): Date {
  if (/[zZ]$|[+-]\d{2}:?\d{2}$/.test(iso)) return new Date(iso);

  return new Date(iso + 'Z');
}

export function parseDate(input: string | number | null | undefined): Date | null {
  return parseInput(input);
}

function parseInput(input: string | number | null | undefined): Date | null {
  if (input === null || input === undefined) return null;
  if (typeof input === 'number') {
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
  hour12: false,
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

export function formatWith(
  input: string | number | null | undefined,
  options: Intl.DateTimeFormatOptions,
  fallback = '—'
): string {
  const d = parseInput(input);
  if (!d || isNaN(d.getTime())) return fallback;
  return new Intl.DateTimeFormat(undefined, { ...options, timeZone: _timezone }).format(d);
}

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

export function epochSecFromLocalDateTimeInput(str: string): number {
  return Math.floor(epochMsFromLocalDateTimeInput(str) / 1000);
}
