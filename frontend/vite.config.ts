import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  publicDir: 'public',
  // Tauri attend http://localhost:1420/ (cf src-tauri/tauri.conf.json devUrl)
  server: {
    port: 1420,
    strictPort: true,
  },
  // Pour éviter les erreurs de CORS en dev
  clearScreen: false,
})
