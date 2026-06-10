export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        background: '#050505',
        surface: 'rgba(35, 32, 45, 0.8)',
        'surface-high': 'rgba(45, 40, 55, 0.9)',
        primary: '#D0BCFF',
        'primary-container': 'rgba(79, 55, 139, 0.9)',
        secondary: '#CCC2DC',
        tertiary: '#EFB8C8',
        error: '#F2B8B5',
        'error-container': 'rgba(140, 29, 24, 0.8)',
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
