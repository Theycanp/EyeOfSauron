import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
export default defineConfig({
  plugins: [react()],
  base: '/',
  build: {
    // Keep the deployable static bundle beside the Python package. The
    // standard-library admin server serves this directory in production.
    outDir: '../src/argus/admin_web',
    emptyOutDir: true,
    sourcemap: false,
    target: 'es2022',
    rollupOptions: {
      output: {
        entryFileNames: 'assets/index.js',
        chunkFileNames: 'assets/[name].js',
        assetFileNames: ({ name }) => name?.endsWith('.css')
          ? 'assets/index.css'
          : 'assets/[name][extname]',
      },
    },
  },
})
