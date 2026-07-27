import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// base: './' is required so the built index.html uses relative asset paths.
// Electron loads the build from a file:// URL, and absolute paths ("/assets/...")
// 404 under file://, which is the classic cause of a blank overlay window.
export default defineConfig({
  base: './',
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true
  }
});
