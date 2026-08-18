/** Verify the runtime and packaging contract without invoking a platform packager. */
import {
  cpSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { basename, dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const packageJson = JSON.parse(readFileSync(resolve(root, 'package.json'), 'utf8'));
const builder = readFileSync(resolve(root, 'electron-builder.yml'), 'utf8');
const mainSource = readFileSync(resolve(root, 'electron/main.ts'), 'utf8');

function verifyMigrationBackupCreation() {
  const fixtureRoot = mkdtempSync(join(tmpdir(), 'studycrew-migration-'));
  try {
    const source = join(fixtureRoot, 'legacy', 'state.json');
    const backupRoot = join(fixtureRoot, 'data', 'migration-backups', 'knowe-fixture');
    mkdirSync(dirname(source), { recursive: true });
    writeFileSync(source, '{"recoverable":true}', { encoding: 'utf8', flag: 'w' });
    mkdirSync(backupRoot, { recursive: true });
    const backup = join(backupRoot, `01-${basename(source)}`);
    cpSync(source, backup, { errorOnExist: true, force: false });
    return existsSync(backup)
      && readFileSync(backup, 'utf8') === '{"recoverable":true}'
      && existsSync(source);
  } finally {
    rmSync(fixtureRoot, { recursive: true, force: true });
  }
}

const localeValues = [
  ...Object.values(JSON.parse(readFileSync(resolve(root, 'src/locales/en.json'), 'utf8'))),
  ...Object.values(JSON.parse(readFileSync(resolve(root, 'src/locales/zh.json'), 'utf8'))),
];
const visibleBrandSources = [
  'src/components/FirstRunModelGate.tsx',
  'src/components/GlobalSearch.tsx',
].map((path) => readFileSync(resolve(root, path), 'utf8'));

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
  ['backup root is created before migration copy', mainSource.indexOf('mkdirSync(backupRoot, { recursive: true });') < mainSource.indexOf('unique.forEach')],
  ['migration backup can be created and copied without an existing parent', verifyMigrationBackupCreation()],
  ['locale values contain no legacy visible brand', localeValues.every((value) => typeof value !== 'string' || !value.includes('Knowe'))],
  ['visible components contain no legacy brand literal', visibleBrandSources.every((source) => !source.includes("'Knowe'") && !source.includes('>Knowe<'))],
];

const failures = checks.filter(([, passed]) => !passed).map(([name]) => name);
if (failures.length) {
  console.error(`❌ runtime verification failed: ${failures.join(', ')}`);
  process.exit(1);
}

console.log('✅ StudyCrew runtime and Windows packaging contract verified');
