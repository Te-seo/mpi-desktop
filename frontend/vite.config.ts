import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Electron main/preload are compiled by tsc (see tsconfig.electron.json + the
// `electron:build` npm script). Vite handles only the React renderer here.
//
// base: './' ships relative asset URLs in dist/index.html so Electron's
// loadFile() (which uses file://) can resolve them. The default '/' would
// look up /assets at the filesystem root and render a blank window.
export default defineConfig({
  base: './',
  plugins: [react()],
  server: {
    port: 5173,
  },
  build: {
    outDir: 'dist',
    rollupOptions: {
      output: {
        // Split heavy third-party libs out of the app chunk so no single file
        // trips Vite's 500 kB warning. recharts (+ its bundled d3 modules) is by
        // far the biggest dependency, so it gets its own chunk.
        manualChunks(id) {
          if (!id.includes('node_modules')) return
          if (/[\\/]node_modules[\\/](recharts|victory-vendor|d3-[^\\/]+)[\\/]/.test(id)) return 'charts'
          if (/[\\/]node_modules[\\/](react|react-dom|react-router|react-router-dom|scheduler)[\\/]/.test(id)) return 'react-vendor'
          return 'vendor'
        },
      },
    },
  },
})
