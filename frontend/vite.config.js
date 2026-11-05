import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The desktop build (`vite build --mode desktop`) is served by the backend itself, so the interface
// calls its own origin; it goes to dist-desktop and leaves the development build alone.
export default defineConfig(({ mode }) => ({
  plugins: [react()],
  define: mode === 'desktop' ? { 'import.meta.env.VITE_API_BASE': JSON.stringify('same-origin') } : {},
  build: mode === 'desktop' ? { outDir: 'dist-desktop', emptyOutDir: true } : {},
}))
