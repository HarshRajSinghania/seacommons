import { cpSync, existsSync, renameSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

// The Vite build now emits both documents (multi-page input in vite.config.ts):
//   dist/index.html  -> operational console  (renamed to console.html here)
//   dist/site.html   -> institutional site   (served at seacommons.org)
//   dist/docs.html   -> canonical public docs (served at /docs)
// vercel.json rewrites map public hosts and documentation paths explicitly.

const webRoot = resolve(fileURLToPath(new URL('..', import.meta.url)));
const distRoot = resolve(webRoot, 'dist');
const cesiumRoot = resolve(webRoot, 'node_modules', 'cesium', 'Build', 'Cesium');

const consoleIndex = resolve(distRoot, 'index.html');
const siteIndex = resolve(distRoot, 'site.html');
const playIndex = resolve(distRoot, 'play.html');
const docsIndex = resolve(distRoot, 'docs.html');

if (!existsSync(consoleIndex)) {
  throw new Error('Vite build is missing dist/index.html (console entry)');
}
if (!existsSync(siteIndex)) {
  throw new Error('Vite build is missing dist/site.html (institutional site entry)');
}
if (!existsSync(playIndex)) {
  throw new Error('Vite build is missing dist/play.html (Play timeline entry)');
}
if (!existsSync(docsIndex)) {
  throw new Error('Vite build is missing dist/docs.html (public docs entry)');
}

renameSync(consoleIndex, resolve(distRoot, 'console.html'));

for (const directory of ['Assets', 'ThirdParty', 'Widgets', 'Workers']) {
  cpSync(resolve(cesiumRoot, directory), resolve(distRoot, 'cesium', directory), { recursive: true });
}

console.log('Unified package ready: institutional site + docs + Play timeline + Live console');
