import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineConfig, type Plugin } from 'vitest/config';

const srcDir = fileURLToPath(new URL('./src', import.meta.url));
const testDir = fileURLToPath(new URL('../tests/frontend', import.meta.url));
const repoRoot = fileURLToPath(new URL('..', import.meta.url));

const bareImportRE = /^(?![a-zA-Z]:)[\w@]/;

const resolveTestDependenciesFromFrontend = (): Plugin => ({
  name: 'thot:resolve-test-dependencies-from-frontend',
  enforce: 'pre',
  async resolveId(source, importer) {
    if (!importer?.startsWith(testDir)) return null;
    if (source.startsWith('@/') || !bareImportRE.test(source)) return null;
    const resolved = await this.resolve(source);
    return resolved?.id ?? null;
  },
});

export default defineConfig({
  plugins: [resolveTestDependenciesFromFrontend()],
  resolve: {
    alias: {
      '@': srcDir,
    },
  },
  server: {
    fs: {
      allow: [repoRoot],
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: join(testDir, 'setup.ts'),
    css: true,
    include: [join(testDir, '**/*.test.tsx'), join(testDir, '**/*.test.ts')],
    testTimeout: 15000,
    hookTimeout: 15000,
  },
});