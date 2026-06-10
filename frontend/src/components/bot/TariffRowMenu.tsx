import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { MoreHorizontal, Copy, EyeOff, RotateCcw, Trash2 } from 'lucide-react';
import { cn } from '@/lib/utils';
import type { Tariff } from '@/lib/types';

interface TariffRowMenuProps {
  tariff: Tariff;
  onDuplicate: () => void;
  onArchive: () => void;
  onRestore: () => void;
  onDelete: () => void;
}

const MENU_ESTIMATED_HEIGHT = 160;
const MENU_WIDTH = 200;

export function TariffRowMenu({
  tariff,
  onDuplicate,
  onArchive,
  onRestore,
  onDelete,
}: TariffRowMenuProps) {
  const [open, setOpen] = useState(false);
  const [menuStyle, setMenuStyle] = useState<React.CSSProperties>({});
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  const updatePosition = () => {
    const rect = triggerRef.current?.getBoundingClientRect();
    if (!rect) return;
    const spaceBelow = window.innerHeight - rect.bottom;
    const openUpward = spaceBelow < MENU_ESTIMATED_HEIGHT + 16 && rect.top > spaceBelow;
    setMenuStyle({
      position: 'fixed',
      left: rect.right - MENU_WIDTH,
      width: MENU_WIDTH,
      zIndex: 10000,
      ...(openUpward ? { bottom: window.innerHeight - rect.top + 6 } : { top: rect.bottom + 6 }),
    });
  };

  useEffect(() => {
    if (!open) return;
    updatePosition();
    const onScrollOrResize = () => updatePosition();
    window.addEventListener('scroll', onScrollOrResize, true);
    window.addEventListener('resize', onScrollOrResize);
    const onClickOutside = (e: MouseEvent) => {
      const target = e.target as Node;
      if (!triggerRef.current?.contains(target) && !menuRef.current?.contains(target)) {
        setOpen(false);
      }
    };
    const onEsc = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onClickOutside);
    document.addEventListener('keydown', onEsc);
    return () => {
      window.removeEventListener('scroll', onScrollOrResize, true);
      window.removeEventListener('resize', onScrollOrResize);
      document.removeEventListener('mousedown', onClickOutside);
      document.removeEventListener('keydown', onEsc);
    };
  }, [open]);

  const isArchived = tariff.visibility === 'archived';

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          setOpen((v) => !v);
        }}
        className={cn(
          'flex h-8 w-8 items-center justify-center rounded-lg border border-white/[0.06] bg-white/[0.04] text-gray-400 transition-colors',
          'hover:border-white/[0.12] hover:bg-white/[0.10] hover:text-white',
          open && 'border-white/[0.12] bg-white/[0.10] text-white'
        )}
        aria-label="Tariff actions"
      >
        <MoreHorizontal size={16} />
      </button>

      {open &&
        createPortal(
          <div
            ref={menuRef}
            style={menuStyle}
            className="overflow-hidden rounded-xl border border-white/[0.10] bg-zinc-950 shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          >
            <MenuItem
              icon={<Copy size={16} />}
              label="Duplicate"
              onClick={() => {
                onDuplicate();
                setOpen(false);
              }}
            />
            {isArchived ? (
              <MenuItem
                icon={<RotateCcw size={16} />}
                label="Restore to public"
                tone="emerald"
                onClick={() => {
                  onRestore();
                  setOpen(false);
                }}
              />
            ) : (
              <MenuItem
                icon={<EyeOff size={16} />}
                label="Archive"
                tone="amber"
                onClick={() => {
                  onArchive();
                  setOpen(false);
                }}
              />
            )}
            <div className="border-t border-white/[0.05]" />
            <MenuItem
              icon={<Trash2 size={16} />}
              label="Delete…"
              tone="rose"
              onClick={() => {
                onDelete();
                setOpen(false);
              }}
            />
          </div>,
          document.body
        )}
    </>
  );
}

interface MenuItemProps {
  icon: React.ReactNode;
  label: string;
  tone?: 'default' | 'amber' | 'emerald' | 'rose';
  onClick: () => void;
}

function MenuItem({ icon, label, tone = 'default', onClick }: MenuItemProps) {
  const toneClasses = {
    default: 'text-white/80 hover:bg-white/[0.06] hover:text-white',
    amber: 'text-amber-300 hover:bg-amber-500/10 hover:text-amber-200',
    emerald: 'text-emerald-300 hover:bg-emerald-500/10 hover:text-emerald-200',
    rose: 'text-rose-300 hover:bg-rose-500/10 hover:text-rose-200',
  };
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        'flex w-full items-center gap-3 px-4 py-2.5 text-left text-sm font-medium transition-colors',
        toneClasses[tone]
      )}
    >
      {icon}
      <span>{label}</span>
    </button>
  );
}
