import { useState, useRef, useEffect, SelectHTMLAttributes } from 'react';
import { cn } from '@/lib/utils';
import { ChevronDown, Check } from 'lucide-react';
import { createPortal } from 'react-dom';
import { motion, AnimatePresence } from 'framer-motion';

interface SelectProps extends Omit<SelectHTMLAttributes<HTMLSelectElement>, 'onChange'> {
  label?: string;
  error?: string;
  options: { value: string | number; label: string }[];
  onChange?: (e: React.ChangeEvent<HTMLSelectElement>) => void;
}

function Select({
  className,
  label,
  error,
  options,
  value,
  onChange,
  disabled,
  name,
}: SelectProps) {
  const [isOpen, setIsOpen] = useState(false);
  const [dropdownStyle, setDropdownStyle] = useState<React.CSSProperties>({});
  const triggerRef = useRef<HTMLButtonElement>(null);
  const dropdownRef = useRef<HTMLDivElement>(null);

  const selectedOption = options.find((o) => String(o.value) === String(value));

  const updatePosition = () => {
    if (!triggerRef.current) return;
    const rect = triggerRef.current.getBoundingClientRect();
    const spaceBelow = window.innerHeight - rect.bottom;
    const dropdownHeight = Math.min(options.length * 42 + 8, 240);
    const openUpward = spaceBelow < dropdownHeight + 8 && rect.top > dropdownHeight;

    setDropdownStyle({
      position: 'fixed',
      left: rect.left,
      width: rect.width,
      zIndex: 9999,
      ...(openUpward ? { bottom: window.innerHeight - rect.top + 4 } : { top: rect.bottom + 4 }),
    });
  };

  const handleToggle = () => {
    if (disabled) return;
    if (!isOpen) updatePosition();
    setIsOpen((v) => !v);
  };

  const handleSelect = (optValue: string | number) => {
    onChange?.({
      target: { value: String(optValue), name: name ?? '' },
    } as React.ChangeEvent<HTMLSelectElement>);
    setIsOpen(false);
  };

  // Close on outside click or Escape
  useEffect(() => {
    if (!isOpen) return;
    const onMouse = (e: MouseEvent) => {
      if (
        !triggerRef.current?.contains(e.target as Node) &&
        !dropdownRef.current?.contains(e.target as Node)
      )
        setIsOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setIsOpen(false);
    };
    document.addEventListener('mousedown', onMouse);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onMouse);
      document.removeEventListener('keydown', onKey);
    };
  }, [isOpen]);

  // Reposition on scroll / resize
  useEffect(() => {
    if (!isOpen) return;
    const handler = () => updatePosition();
    window.addEventListener('scroll', handler, true);
    window.addEventListener('resize', handler);
    return () => {
      window.removeEventListener('scroll', handler, true);
      window.removeEventListener('resize', handler);
    };
  }, [isOpen]);

  return (
    <div className="w-full space-y-1">
      {label && (
        <label className="text-xs font-bold uppercase tracking-wider text-gray-400 ml-1">
          {label}
        </label>
      )}

      <button
        ref={triggerRef}
        type="button"
        onClick={handleToggle}
        disabled={disabled}
        className={cn(
          'flex h-11 w-full items-center justify-between rounded-lg border bg-black/20 px-3 py-2 text-sm backdrop-blur-sm transition-colors text-left',
          'disabled:cursor-not-allowed disabled:opacity-50',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:border-transparent',
          isOpen
            ? 'border-primary/60 ring-2 ring-primary/30'
            : error
              ? 'border-error'
              : 'border-white/10 hover:border-white/20',
          className
        )}
      >
        <span className={selectedOption ? 'text-gray-200' : 'text-gray-500'}>
          {selectedOption?.label ?? 'Select...'}
        </span>
        <motion.span
          animate={{ rotate: isOpen ? 180 : 0 }}
          transition={{ duration: 0.18, ease: 'easeOut' }}
          className="ml-2 shrink-0 text-gray-400"
        >
          <ChevronDown size={15} />
        </motion.span>
      </button>

      {error && <span className="text-xs text-error ml-1">{error}</span>}

      {createPortal(
        <AnimatePresence>
          {isOpen && (
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
                {options.map((opt) => {
                  const isSelected = String(value) === String(opt.value);
                  return (
                    <button
                      key={opt.value}
                      type="button"
                      onClick={() => handleSelect(opt.value)}
                      className={cn(
                        'flex w-full items-center justify-between px-3 py-2.5 text-sm transition-colors',
                        isSelected
                          ? 'bg-primary/15 text-primary'
                          : 'text-gray-300 hover:bg-white/[0.06] hover:text-white'
                      )}
                    >
                      {opt.label}
                      {isSelected && <Check size={13} className="shrink-0 opacity-70" />}
                    </button>
                  );
                })}
              </div>
            </motion.div>
          )}
        </AnimatePresence>,
        document.body
      )}
    </div>
  );
}

Select.displayName = 'Select';
export { Select };
