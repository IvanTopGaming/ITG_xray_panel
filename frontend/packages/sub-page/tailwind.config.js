export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        background: '#050505',
        surface: 'rgba(35, 32, 45, 0.8)',
        primary: '#D0BCFF',
        'primary-container': 'rgba(79, 55, 139, 0.9)',
        secondary: '#CCC2DC',
        muted: '#9a90ad',
        ok: '#7ee787',
        warn: '#fbbf24',
        error: '#f87171',
      },
      fontFamily: {
        sans: ['Roboto', 'sans-serif'],
        mono: ['Roboto Mono', 'monospace'],
      },
      backgroundImage: {
        'main-gradient':
          'radial-gradient(circle at 15% 15%, rgba(46, 16, 101, 0.4) 0%, transparent 40%), radial-gradient(circle at 85% 85%, rgba(76, 29, 149, 0.3) 0%, transparent 40%)',
      },
    },
  },
  plugins: [],
};
