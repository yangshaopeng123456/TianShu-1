// 2026-10-16: invoke the project's Python installer without shell quoting.
const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');
const python = path.resolve(__dirname, '..', 'dpf_agent', '.venv', 'Scripts', 'python.exe');
const installer = path.resolve(__dirname, '..', 'scripts', 'install-electron.py');
if (!fs.existsSync(python)) {
  console.error('Create dpf_agent/.venv and install the locked backend dependencies first; see README.md.');
  process.exit(1);
}
const result = spawnSync(python, [installer], { stdio: 'inherit', windowsHide: true });
if (result.error) console.error('Could not start the project Python installer.');
process.exit(result.status ?? 1);
