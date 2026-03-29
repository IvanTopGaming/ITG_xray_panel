import { useEffect, useState } from 'react';
import { motion, useReducedMotion } from 'framer-motion';

const NOISE_BG = `url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='200' height='200'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.75' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='200' height='200' filter='url(%23n)'/%3E%3C/svg%3E")`;

export function AnimatedBackground() {
  const shouldReduceMotion = useReducedMotion();
  const [isVisible, setIsVisible] = useState(!document.hidden);

  useEffect(() => {
    const handler = () => setIsVisible(!document.hidden);
    document.addEventListener('visibilitychange', handler);
    return () => document.removeEventListener('visibilitychange', handler);
  }, []);

  const animate = !shouldReduceMotion && isVisible;

  return (
    <div className="fixed inset-0 z-[-1] overflow-hidden pointer-events-none">
      {/* Base */}
      <div className="absolute inset-0 bg-[#050505]" />

      {/* Blob 1 — top-left purple */}
      <motion.div
        animate={
          animate
            ? {
                scale: [1, 1.2, 1],
                opacity: [0.25, 0.45, 0.25],
                x: [0, 80, 0],
                y: [0, -40, 0],
              }
            : { scale: 1, opacity: 0.25 }
        }
        transition={{ duration: 20, repeat: Infinity, ease: 'easeInOut' }}
        className="absolute top-[-10%] left-[-10%] w-[500px] h-[500px] bg-primary/20 rounded-full blur-[120px]"
      />

      {/* Blob 2 — bottom-right violet */}
      <motion.div
        animate={
          animate
            ? {
                scale: [1, 1.15, 1],
                opacity: [0.15, 0.3, 0.15],
                x: [0, -40, 0],
                y: [0, 80, 0],
              }
            : { scale: 1, opacity: 0.15 }
        }
        transition={{ duration: 15, repeat: Infinity, ease: 'easeInOut', delay: 2 }}
        className="absolute bottom-[-10%] right-[-10%] w-[600px] h-[600px] bg-secondary/10 rounded-full blur-[120px]"
      />

      {/* Blob 3 — center deep purple */}
      <motion.div
        animate={
          animate
            ? {
                scale: [1, 1.25, 1],
                opacity: [0.08, 0.22, 0.08],
              }
            : { scale: 1, opacity: 0.08 }
        }
        transition={{ duration: 18, repeat: Infinity, ease: 'easeInOut', delay: 5 }}
        className="absolute top-[30%] left-[35%] w-[800px] h-[800px] bg-[#4f378b]/15 rounded-full blur-[150px]"
      />

      {/* Dot grid */}
      <div
        className="absolute inset-0"
        style={{
          backgroundImage: 'radial-gradient(circle, rgba(208,188,255,0.07) 1px, transparent 1px)',
          backgroundSize: '36px 36px',
        }}
      />

      {/* Noise texture */}
      <div
        style={{ backgroundImage: NOISE_BG }}
        className="absolute inset-0 opacity-[0.15] brightness-100 contrast-150 mix-blend-overlay"
      />
    </div>
  );
}
