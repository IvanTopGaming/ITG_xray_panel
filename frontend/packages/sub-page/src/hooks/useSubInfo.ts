import { useCallback, useEffect, useRef, useState } from 'react';
import { isSubInfo, type SubInfo, type SubInfoError } from '@/lib/types';

const REQUEST_TIMEOUT_MS = 10000;
const REFRESH_INTERVAL_MS = 60000;

function infoUrl(): string {
  return `${window.location.pathname.replace(/\/+$/, '')}/info${window.location.search}`;
}

export function useSubInfo() {
  const [data, setData] = useState<SubInfo | null>(null);
  const [error, setError] = useState<SubInfoError | null>(null);
  const [loading, setLoading] = useState(true);

  const attempt = useRef<AbortController | null>(null);
  const timeout = useRef<number | undefined>(undefined);

  const load = useCallback(() => {
    attempt.current?.abort();
    window.clearTimeout(timeout.current);
    const controller = new AbortController();
    attempt.current = controller;
    const timer = window.setTimeout(() => {
      controller.abort();
      if (attempt.current === controller) {
        setData(null);
        setError('unavailable');
        setLoading(false);
      }
    }, REQUEST_TIMEOUT_MS);
    timeout.current = timer;
    const current = () => attempt.current === controller;

    setLoading(true);
    setError(null);
    fetch(infoUrl(), {
      headers: { Accept: 'application/json' },
      signal: controller.signal,
      cache: 'no-store',
    })
      .then((response) => {
        if (!response.ok) throw new Error(response.status === 404 ? 'not_found' : 'unavailable');
        return response.json();
      })
      .then((body: unknown) => {
        if (controller.signal.aborted) throw new Error('unavailable');
        if (!isSubInfo(body)) throw new Error('invalid_response');
        if (current()) setData(body);
      })
      .catch((failure: unknown) => {
        if (current()) {
          const kind = failure instanceof Error ? failure.message : '';
          setError(kind === 'not_found' || kind === 'invalid_response' ? kind : 'unavailable');
          setData(null);
        }
      })
      .finally(() => {
        window.clearTimeout(timer);
        if (current()) setLoading(false);
      });
  }, []);

  useEffect(() => {
    load();
    const interval = window.setInterval(load, REFRESH_INTERVAL_MS);
    window.addEventListener('focus', load);
    return () => {
      attempt.current?.abort();
      attempt.current = null;
      window.clearTimeout(timeout.current);
      window.clearInterval(interval);
      window.removeEventListener('focus', load);
    };
  }, [load]);

  useEffect(() => {
    const expiry = data?.expiry_at;
    if (data?.status !== 'active' || expiry == null || expiry <= Date.now()) return;
    const timer = window.setTimeout(load, Math.min(expiry - Date.now() + 50, 2147483647));
    return () => window.clearTimeout(timer);
  }, [data, load]);

  return { data, error, loading, reload: load };
}
