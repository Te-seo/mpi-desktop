import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

// Renderer unit/component tests. Kept separate from vite.config.ts (the Electron
// build config) so test-only settings never leak into the production bundle.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    include: [
      'src/**/*.{test,spec}.{ts,tsx}',
      'electron/**/*.{test,spec}.ts',
    ],
    css: false,
  },
})
