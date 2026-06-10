import { create } from 'zustand';
import { useAuthStore } from './authStore';
import { panelBase } from '@/lib/panelBase';

const MAX_LOG_LINES = 2000;
const normalizedBase = panelBase.endsWith('/') ? panelBase : `${panelBase}/`;
const LOGS_ENDPOINT = `${normalizedBase}api/logs`;

let _abortController: AbortController | null = null;

export interface LogEntry {
  text: string;
  ts: number;
}

function parseSseEventChunk(chunk: string): string[] {
  return chunk
    .split('\n')
    .filter((line) => line.startsWith('data:'))
    .map((line) => line.slice(5).trimStart())
    .filter(Boolean);
}

interface LogState {
  logs: LogEntry[];
  isStreaming: boolean;
  toggleStream: () => Promise<void>;
  clearLogs: () => void;
}

export const useLogStore = create<LogState>((set, get) => ({
  logs: [{ text: 'System ready. Waiting for logs...', ts: Date.now() }],
  isStreaming: false,

  toggleStream: async () => {
    const { isStreaming } = get();
    const token = useAuthStore.getState().token;

    if (isStreaming) {
      if (_abortController) {
        _abortController.abort();
        _abortController = null;
      }
      set((state) => ({
        isStreaming: false,
        logs: [...state.logs, { text: '> Stream stopped by user.', ts: Date.now() }],
      }));
      return;
    }

    if (!token) {
      set((state) => ({
        isStreaming: false,
        logs: [...state.logs, { text: '> Authentication required.', ts: Date.now() }],
      }));
      return;
    }

    set((state) => ({
      isStreaming: true,
      logs: [
        ...state.logs,
        { text: '> Initializing Xray/container log stream...', ts: Date.now() },
      ],
    }));

    _abortController = new AbortController();

    try {
      const response = await fetch(LOGS_ENDPOINT, {
        headers: {
          Authorization: `Bearer ${token}`,
          Accept: 'text/event-stream',
        },
        cache: 'no-store',
        signal: _abortController.signal,
      });

      if (!response.ok) {
        throw new Error(`Stream request failed (${response.status})`);
      }
      if (!response.body) {
        throw new Error('Stream body is empty');
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const events = buffer.split('\n\n');
        buffer = events.pop() || '';

        const lines = events.flatMap(parseSseEventChunk);
        if (lines.length) {
          const now = Date.now();
          set((state) => ({
            logs: [...state.logs, ...lines.map((text) => ({ text, ts: now }))].slice(
              -MAX_LOG_LINES
            ),
          }));
        }
      }

      buffer += decoder.decode();
      const tailLines = parseSseEventChunk(buffer);
      if (tailLines.length) {
        const now = Date.now();
        set((state) => ({
          logs: [...state.logs, ...tailLines.map((text) => ({ text, ts: now }))].slice(
            -MAX_LOG_LINES
          ),
        }));
      }

      set((state) => ({
        isStreaming: false,
        logs: [...state.logs, { text: '> Stream finished.', ts: Date.now() }],
      }));
    } catch (e: any) {
      if (e.name === 'AbortError') {
        set({ isStreaming: false });
        return;
      }
      set((state) => ({
        isStreaming: false,
        logs: [
          ...state.logs,
          { text: `> Stream error: ${e.message || 'connection lost'}`, ts: Date.now() },
        ],
      }));
    } finally {
      _abortController = null;
    }
  },

  clearLogs: () => set({ logs: [] }),
}));
