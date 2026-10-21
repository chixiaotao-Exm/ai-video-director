import {defineConfig} from 'vite';
import vue from '@vitejs/plugin-vue';
import {fileURLToPath, URL} from 'node:url';
import {readFileSync} from 'node:fs';

const target = 'http://127.0.0.1:8000';
const proxy = Object.fromEntries([
  '/static', '/dashboard', '/projects', '/characters', '/voices', '/singing-models', '/uploads', '/media', '/media-assets',
  '/jobs', '/costs', '/settings', '/system', '/workers', '/health', '/metrics', '/version-control'
].map(path => [path, {
  target, changeOrigin: true,
  configure(server) {
    server.on('proxyReq', (request, original) => {
      // Only our own local development origin may be rewritten for the backend.
      if (['http://127.0.0.1:5173', 'http://localhost:5173'].includes(original.headers.origin)) {
        request.setHeader('Origin', target);
      }
    });
  }
}]));

export default defineConfig(({command}) => ({
  plugins: [vue(), {
    name: 'vue-license-notice',
    generateBundle() {
      this.emitFile({type:'asset', fileName:'LICENSES.txt', source:readFileSync(new URL('./node_modules/vue/LICENSE',import.meta.url),'utf8')});
    }
  }],
  base: command === 'serve' ? '/' : '/static/studio/',
  server: {port: 5173, strictPort: true, proxy},
  build: {
    outDir: fileURLToPath(new URL('../src/avd/api/static/studio', import.meta.url)),
    emptyOutDir: true,
    cssCodeSplit: false,
    rollupOptions: {
      input: fileURLToPath(new URL('./src/main.js', import.meta.url)),
      output: {entryFileNames: 'studio.js', chunkFileNames: '[name]-[hash].js', assetFileNames: 'studio.[ext]'}
    }
  }
}));
