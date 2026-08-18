/** Verify the runtime and packaging contract without invoking a platform packager. */
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const packageJson = JSON.parse(readFileSync(resolve(root, 'package.json'), 'utf8'));
const builder = readFileSync(resolve(root, 'electron-builder.yml'), 'utf8');

const checks = [
  ['package name', packageJson.name === 'studycrew-desktop'],
  ['Node 24 engine', packageJson.engines?.node === '>=24 <25'],
  ['React 18 retained', /^\^?18\./.test(packageJson.dependencies?.react ?? '')],
  ['React DOM 18 retained', /^\^?18\./.test(packageJson.dependencies?.['react-dom'] ?? '')],
  ['Electron 43 pinned', /^43\.\d+\.\d+$/.test(packageJson.devDependencies?.electron ?? '')],
  ['Node 24 types pinned', /^24\.\d+\.\d+$/.test(packageJson.devDependencies?.['@types/node'] ?? '')],
  ['Windows app id', /appId: io\.studycrew\.desktop/.test(builder)],
  ['StudyCrew product name', /productName: StudyCrew 学伴团/.test(builder)],
  ['Windows x64 only', /target:\s*\n\s*- target: nsis\s*\n\s*arch:\s*\n\s*- x64/.test(builder) && !/^mac:/m.test(builder)],
  ['StudyCrew artifact', /artifactName: StudyCrew-Setup-\$\{version\}\.\$\{ext\}/.test(builder)],
  ['unsigned runtime updates disabled', /signedAutoUpdateEnabled = false/.test(readFileSync(resolve(root, 'electron/updater.ts'), 'utf8'))],
];

const failures = checks.filter(([, passed]) => !passed).map(([name]) => name);
if (failures.length) {
  console.error(`❌ runtime verification failed: ${failures.join(', ')}`);
  process.exit(1);
}

console.log('✅ StudyCrew runtime and Windows packaging contract verified');
