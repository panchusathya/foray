import { cpSync, existsSync, mkdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

const here = dirname(fileURLToPath(import.meta.url));
const webRoot = join(here, '..');
const widgetRoot = join(webRoot, '..', 'widget');
const widgetDist = join(widgetRoot, 'dist', 'juicebox-intake-widget.js');
const publicDir = join(webRoot, 'public');

function run(command, args, cwd) {
  const result = spawnSync(command, args, { cwd, stdio: 'inherit' });
  if (result.status !== 0) {
    process.exit(result.status ?? 1);
  }
}

if (!existsSync(join(widgetRoot, 'node_modules'))) {
  run('npm', ['ci', '--no-audit', '--no-fund'], widgetRoot);
}

run('npm', ['run', 'build'], widgetRoot);

if (!existsSync(widgetDist)) {
  console.error(`widget build missing: ${widgetDist}`);
  process.exit(1);
}

mkdirSync(publicDir, { recursive: true });
cpSync(widgetDist, join(publicDir, 'juicebox-intake-widget.js'));
