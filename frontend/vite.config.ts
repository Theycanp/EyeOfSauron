import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  base: '/',
  build: {
    outDir: '../src/argus/admin_web',
    emptyOutDir: true,
    sourcemap: false,
    target: 'es2022',
  },
})
