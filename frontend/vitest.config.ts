import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    css: false,
    // React 19.2 removed `act` from the production build; under NODE_ENV=production
    // (vitest's default) @testing-library/react throws "React.act is not a function".
    // Force the development build so `act` exists for tests.
    env: {
      NODE_ENV: 'development',
    },
  },
});