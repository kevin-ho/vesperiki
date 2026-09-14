import type { Config } from 'tailwindcss';

export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        bg: 'var(--color-bg)', surface: 'var(--color-surface)', text: 'var(--color-text)',
        muted: 'var(--color-muted)', border: 'var(--color-border)', peach: 'var(--color-peach)',
        butter: 'var(--color-butter)', sage: 'var(--color-sage)', rose: 'var(--color-rose)', lavender: 'var(--color-lavender)',
        peachText: 'var(--color-peach-text)', butterText: 'var(--color-butter-text)', sageText: 'var(--color-sage-text)', roseText: 'var(--color-rose-text)', lavenderText: 'var(--color-lavender-text)',
      },
      boxShadow: { brutal: 'var(--shadow-brutal)' },
      fontFamily: { display: 'var(--font-display)', body: 'var(--font-body)', mono: 'var(--font-mono)' },
    },
  },
} satisfies Config;
