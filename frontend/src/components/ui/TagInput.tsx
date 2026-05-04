import { KeyboardEvent, useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { AnimatePresence, motion } from 'framer-motion';
import { X } from 'lucide-react';
import { cn } from '@/lib/utils';

interface TagInputProps {
  label?: string;
  value: string[];
  onChange: (tags: string[]) => void;
  suggestions?: string[];
  placeholder?: string;
  helperText?: string;
  maxLength?: number;
  pattern?: RegExp | null;
  patternError?: string;
}

const TAG_RE = /^[A-Za-z0-9_-]+$/;

// Distinct pill palettes — picked deterministically per tag so the same tag
// always renders in the same color across the app.
const TAG_PALETTES = [
  'bg-primary/15 border-primary/40 text-primary',
  'bg-violet-500/15 border-violet-400/40 text-violet-200',
  'bg-sky-500/15 border-sky-400/40 text-sky-200',
  'bg-emerald-500/15 border-emerald-400/40 text-emerald-200',
  'bg-amber-500/15 border-amber-400/40 text-amber-200',
  'bg-pink-500/15 border-pink-400/40 text-pink-200',
  'bg-cyan-500/15 border-cyan-400/40 text-cyan-200',
  'bg-rose-500/15 border-rose-400/40 text-rose-200',
];

function tagPalette(tag: string): string {
  let hash = 0;
  for (let i = 0; i < tag.length; i++) {
    hash = (hash * 31 + tag.charCodeAt(i)) | 0;
  }
  return TAG_PALETTES[Math.abs(hash) % TAG_PALETTES.length];
}

export function TagInput({
  label,
  value,
  onChange,
  suggestions = [],
  placeholder = 'Add tag…',
  helperText,
  maxLength = 30,
  pattern = TAG_RE,
  patternError,
}: TagInputProps) {
  const [draft, setDraft] = useState('');
  const [focused, setFocused] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dropdownStyle, setDropdownStyle] = useState<React.CSSProperties>({});
  const inputRef = useRef<HTMLInputElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const dropdownRef = useRef<HTMLDivElement>(null);

  const filteredSuggestions = useMemo(() => {
    const lower = draft.trim().toLowerCase();
    return suggestions.filter(
      (s) => !value.includes(s) && (lower === '' || s.toLowerCase().includes(lower))
    );
  }, [suggestions, draft, value]);

  const showDropdown = focused && filteredSuggestions.length > 0;

  const updatePosition = () => {
    if (!containerRef.current) return;
    const rect = containerRef.current.getBoundingClientRect();
    const dropdownHeight = Math.min(filteredSuggestions.length * 38 + 12, 240);
    const spaceBelow = window.innerHeight - rect.bottom;
    const openUpward = spaceBelow < dropdownHeight + 8 && rect.top > dropdownHeight;

    setDropdownStyle({
      position: 'fixed',
      left: rect.left,
      width: rect.width,
      zIndex: 9999,
      ...(openUpward ? { bottom: window.innerHeight - rect.top + 4 } : { top: rect.bottom + 4 }),
    });
  };

  useEffect(() => {
    if (!showDropdown) return;
    updatePosition();
    const handler = () => updatePosition();
    window.addEventListener('scroll', handler, true);
    window.addEventListener('resize', handler);
    return () => {
      window.removeEventListener('scroll', handler, true);
      window.removeEventListener('resize', handler);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showDropdown, filteredSuggestions.length]);

  const addTag = (raw: string) => {
    const tag = raw.trim();
    if (!tag) return;
    if (tag.length > maxLength) {
      setError(`Tag must be ≤ ${maxLength} chars`);
      return;
    }
    if (pattern && !pattern.test(tag)) {
      setError(patternError || "Use only letters, digits, '-', '_'");
      return;
    }
    if (value.includes(tag)) {
      setDraft('');
      return;
    }
    onChange([...value, tag]);
    setDraft('');
    setError(null);
  };

  const removeTag = (tag: string) => {
    onChange(value.filter((t) => t !== tag));
  };

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter' || e.key === ',') {
      e.preventDefault();
      addTag(draft);
    } else if (e.key === 'Backspace' && draft === '' && value.length > 0) {
      e.preventDefault();
      removeTag(value[value.length - 1]);
    } else if (e.key === 'Escape') {
      setFocused(false);
      inputRef.current?.blur();
    }
  };

  return (
    <div className="w-full space-y-1">
      {label && (
        <label className="text-xs font-bold uppercase tracking-wider text-gray-400 ml-1">
          {label}
        </label>
      )}
      <div
        ref={containerRef}
        className={cn(
          'relative min-h-11 w-full rounded-lg border border-white/10 bg-black/20 px-2 py-1.5 backdrop-blur-sm transition-all',
          'focus-within:ring-2 focus-within:ring-primary focus-within:border-transparent',
          error && 'border-error focus-within:ring-error'
        )}
        onClick={() => inputRef.current?.focus()}
      >
        <div className="flex flex-wrap gap-1.5 items-center">
          {value.map((tag) => (
            <span
              key={tag}
              className={cn(
                'inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-xs font-medium shadow-sm',
                tagPalette(tag)
              )}
            >
              {tag}
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation();
                  removeTag(tag);
                }}
                className="opacity-70 hover:opacity-100 hover:text-white transition-all"
                aria-label={`Remove ${tag}`}
              >
                <X size={12} />
              </button>
            </span>
          ))}
          <input
            ref={inputRef}
            value={draft}
            onChange={(e) => {
              const v = e.target.value;
              setError(null);
              if (/[,\n\t]/.test(v)) {
                const parts = v.split(/[,\n\t]+/);
                const last = parts.pop() ?? '';
                parts.forEach((p) => addTag(p));
                setDraft(last);
              } else {
                setDraft(v);
              }
            }}
            onKeyDown={onKeyDown}
            onFocus={() => setFocused(true)}
            onBlur={() => {
              setTimeout(() => setFocused(false), 150);
              if (draft.trim()) addTag(draft);
            }}
            placeholder={value.length === 0 ? placeholder : ''}
            className="flex-1 min-w-[80px] bg-transparent text-sm outline-none placeholder:text-gray-500 px-1 py-1"
          />
        </div>
      </div>
      {error ? (
        <span className="text-xs text-error ml-1">{error}</span>
      ) : helperText ? (
        <span className="text-xs text-gray-500 ml-1">{helperText}</span>
      ) : null}

      {createPortal(
        <AnimatePresence>
          {showDropdown && (
            <motion.div
              ref={dropdownRef}
              style={{ ...dropdownStyle, transformOrigin: 'top' }}
              initial={{ opacity: 0, scaleY: 0.92, y: -4 }}
              animate={{ opacity: 1, scaleY: 1, y: 0 }}
              exit={{ opacity: 0, scaleY: 0.94, y: -4 }}
              transition={{ duration: 0.14, ease: 'easeOut' }}
              className="rounded-xl border border-white/[0.1] bg-[#18151f]/95 backdrop-blur-2xl shadow-[0_8px_40px_rgba(0,0,0,0.7),0_0_0_1px_rgba(255,255,255,0.04)] overflow-hidden"
            >
              <div className="py-1.5 max-h-60 overflow-y-auto custom-scrollbar">
                {filteredSuggestions.map((s) => (
                  <button
                    key={s}
                    type="button"
                    onMouseDown={(e) => {
                      e.preventDefault();
                      addTag(s);
                    }}
                    className="flex w-full items-center gap-2 px-3 py-2 text-sm text-gray-200 hover:bg-white/[0.06] hover:text-white transition-colors"
                  >
                    <span
                      className={cn(
                        'inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium',
                        tagPalette(s)
                      )}
                    >
                      {s}
                    </span>
                  </button>
                ))}
              </div>
            </motion.div>
          )}
        </AnimatePresence>,
        document.body
      )}
    </div>
  );
}
