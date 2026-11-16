#!/usr/bin/env node
/**
 * Open the existing official DeepSeek Harness desktop application.
 * Author: yangshaopeng. No installation, policy/profile changes or old backend.
 * --check-only locates an executable; it does not establish UI/plugin readiness.
 */
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const { spawn, spawnSync } = require('node:child_process');

function result(checkOnly) {
  return {
    schema: 'tianshu-harness-launch/1', author: 'yangshaopeng',
    check_only: checkOnly, status: 'not_started', launched: false,
    executable: null, process_id: null,
    workspace_selected: false, agent_preset_selected: false,
    plugin_installation_checked: false, old_tianshu_backend_started: false,
    instructions: 'Harness keeps its own session restore behavior. Select the project workspace and Tianshu distribution preset in Harness when needed.',
  };
}
function emit(value, code = 0) {
  process.stdout.write(JSON.stringify(value) + '\n');
  process.exitCode = code;
}
function argumentsOf(argv) {
  let checkOnly = false, explicit;
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === '--check-only') { checkOnly = true; continue; }
    if (arg === '--help' || arg === '-h') return { checkOnly, help: true };
    if (arg === '--harness-exe' || arg.startsWith('--harness-exe=')) {
      if (explicit !== undefined) throw new Error('Pass --harness-exe only once.');
      explicit = arg === '--harness-exe' ? argv[++i] : arg.slice('--harness-exe='.length);
      if (typeof explicit !== 'string' || !explicit.trim() || explicit.startsWith('--')) throw new Error('--harness-exe requires an existing executable path.');
      continue;
    }
    throw new Error('Unknown argument: ' + arg);
  }
  return { checkOnly, explicit };
}
function expandEnvironment(value) {
  const variables = new Map(Object.entries(process.env).map(([key, item]) => [key.toUpperCase(), item]));
  return value.replace(/%([^%]+)%/g, (whole, name) => variables.get(name.toUpperCase()) ?? whole);
}
function executableInfo(candidate, source, metadata = {}) {
  if (typeof candidate !== 'string' || !candidate.trim()) return null;
  try {
    let selected = expandEnvironment(candidate.trim());
    if (selected.startsWith('"') && selected.endsWith('"')) selected = selected.slice(1, -1);
    if (!/^DeepSeek(?:\s+|-)?Harness\.exe$/i.test(path.basename(selected))) return null;
    const filename = fs.realpathSync(selected);
    if (!fs.statSync(filename).isFile()) return null;
    return {
      path: filename, source,
      installed_version: typeof metadata.DisplayVersion === 'string' ? metadata.DisplayVersion : null,
      version_source: metadata.DisplayVersion ? 'uninstall_display_version' : null,
      identity_basis: 'expected_executable_filename_and_installed_path_metadata',
      binary_signature_verified: false,
    };
  } catch { return null; }
}
function readCommand(exe, args, options = {}) {
  const output = spawnSync(exe, args, {
    shell: false, windowsHide: true, timeout: 15000, maxBuffer: 4 * 1024 * 1024,
    encoding: null, ...options,
  });
  if (output.error || output.status !== 0 || !Buffer.isBuffer(output.stdout)) return null;
  return output.stdout;
}
function registryDecoder() {
  const key = 'HKLM\\SYSTEM\\CurrentControlSet\\Control\\Nls\\CodePage';
  const raw = readCommand('reg.exe', ['query', key, '/v', 'OEMCP']);
  const number = raw?.toString('ascii').match(/OEMCP\s+REG_SZ\s+(\d+)/i)?.[1];
  const labels = { '65001': 'utf-8', '936': 'gbk', '950': 'big5', '932': 'shift_jis', '949': 'euc-kr', '866': 'ibm866', '1251': 'windows-1251', '1252': 'windows-1252' };
  const decoder = new TextDecoder(labels[number] ?? 'utf-8');
  return bytes => {
    if (bytes.length >= 2 && bytes[0] === 0xff && bytes[1] === 0xfe) return new TextDecoder('utf-16le').decode(bytes);
    return decoder.decode(bytes);
  };
}
function displayIconPath(value) {
  if (typeof value !== 'string') return '';
  return value.trim().replace(/,\s*-?\d+\s*$/, '').replace(/^"(.*)"$/, '$1');
}
function registryCandidates() {
  const decode = registryDecoder();
  const roots = [
    'HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall',
    'HKLM\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall',
    'HKLM\\Software\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall',
  ];
  const found = [];
  for (const root of roots) {
    const raw = readCommand('reg.exe', ['query', root, '/s', '/f', 'DeepSeek Harness', '/d']);
    if (!raw) continue;
    const keys = [...new Set(decode(raw).split(/\r?\n/).map(line => line.trim()).filter(line => line.startsWith('HKEY_')))];
    for (const key of keys) {
      const valuesRaw = readCommand('reg.exe', ['query', key]);
      if (!valuesRaw) continue;
      const metadata = {};
      for (const line of decode(valuesRaw).split(/\r?\n/)) {
        const match = line.match(/^\s*(DisplayName|DisplayVersion|DisplayIcon|InstallLocation)\s+REG_(?:EXPAND_)?SZ\s+(.*)$/i);
        if (match) {
          const keyName = { displayname: 'DisplayName', displayversion: 'DisplayVersion', displayicon: 'DisplayIcon', installlocation: 'InstallLocation' }[match[1].toLowerCase()];
          metadata[keyName] = match[2].trim();
        }
      }
      if (!/^DeepSeek(?:\s+|-)?Harness(?:\s|$)/i.test(metadata.DisplayName ?? '')) continue;
      const fromIcon = executableInfo(displayIconPath(metadata.DisplayIcon), 'uninstall_display_icon', metadata);
      if (fromIcon) found.push(fromIcon);
      if (metadata.InstallLocation) {
        const fromDirectory = executableInfo(path.join(expandEnvironment(metadata.InstallLocation), 'DeepSeek Harness.exe'), 'uninstall_install_location', metadata);
        if (fromDirectory) found.push(fromDirectory);
      }
    }
  }
  return found;
}
function shortcutCandidates() {
  const systemRoot = process.env.SystemRoot;
  if (!systemRoot) return [];
  // A fixed metadata-only command, not a .ps1 policy bypass. It never reads or
  // evaluates launcher source, executes shortcut arguments or saves a shortcut.
  const command = [
    '[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)',
    '$ErrorActionPreference = "Stop"',
    '$desktopRoots = @([Environment]::GetFolderPath("DesktopDirectory"),[Environment]::GetFolderPath("CommonDesktopDirectory")) | Where-Object { $_ } | Select-Object -Unique',
    '$programRoots = @([Environment]::GetFolderPath("Programs"),[Environment]::GetFolderPath("CommonPrograms")) | Where-Object { $_ } | Select-Object -Unique',
    '$targets = New-Object System.Collections.Generic.List[string]',
    '$shell = $null',
    '$links = @(); foreach ($root in $desktopRoots) { if (Test-Path -LiteralPath $root -PathType Container) { $links += @(Get-ChildItem -LiteralPath $root -Filter "*Harness*.lnk" -File -ErrorAction SilentlyContinue) } }; foreach ($root in $programRoots) { if (Test-Path -LiteralPath $root -PathType Container) { $links += @(Get-ChildItem -LiteralPath $root -Filter "*Harness*.lnk" -File -Recurse -ErrorAction SilentlyContinue) } }',
    'try { $shell = New-Object -ComObject WScript.Shell; foreach ($link in $links) { $target = [string]$shell.CreateShortcut($link.FullName).TargetPath; if ($target) { $targets.Add($target) } } } finally { if ($null -ne $shell) { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell) } }',
    'ConvertTo-Json -InputObject @($targets.ToArray()) -Compress',
  ].join('; ');
  const raw = readCommand(path.join(systemRoot, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe'), ['-NoProfile', '-NonInteractive', '-Command', command]);
  if (!raw) return [];
  try {
    const targets = JSON.parse(raw.toString('utf8').replace(/^\uFEFF/, ''));
    if (!Array.isArray(targets)) return [];
    return targets.map(target => executableInfo(target, 'desktop_or_start_menu_shortcut')).filter(Boolean);
  } catch { return []; }
}
function findHarness() {
  const registered = registryCandidates();
  if (registered.length) return registered[0];
  const common = [
    process.env.LOCALAPPDATA && path.join(process.env.LOCALAPPDATA, 'Programs', 'DeepSeek Harness', 'DeepSeek Harness.exe'),
    process.env.ProgramFiles && path.join(process.env.ProgramFiles, 'DeepSeek Harness', 'DeepSeek Harness.exe'),
    process.env['ProgramFiles(x86)'] && path.join(process.env['ProgramFiles(x86)'], 'DeepSeek Harness', 'DeepSeek Harness.exe'),
  ];
  for (const candidate of common) {
    const info = executableInfo(candidate, 'ordinary_install_directory');
    if (info) return info;
  }
  return shortcutCandidates()[0] ?? null;
}

function main(argv) {
  let args;
  try { args = argumentsOf(argv); }
  catch (error) { emit({ ...result(argv.includes('--check-only')), status: 'invalid_arguments', error: error.message }, 2); return; }
  if (args.help) {
    emit({ schema: 'tianshu-harness-launch-help/1', usage: 'node scripts/start-harness.js [--harness-exe <existing Harness.exe>] [--check-only]', check_only: 'Read-only path/version-metadata probe. Does not launch or check UI/plugin/preset readiness.' });
    return;
  }
  const value = result(args.checkOnly);
  if (process.platform !== 'win32') { emit({ ...value, status: 'unsupported_platform', error: 'This launcher supports Windows Harness only.' }, 5); return; }
  const found = args.explicit === undefined ? findHarness() : executableInfo(args.explicit, 'explicit_parameter');
  if (!found) {
    emit({ ...value, status: args.explicit === undefined ? 'not_found' : 'invalid_explicit_executable', error: args.explicit === undefined
      ? 'DeepSeek Harness was not found. Install it separately or pass --harness-exe with its existing executable path. Nothing was downloaded or installed.'
      : '--harness-exe must name an existing DeepSeek Harness .exe file; pass the complete path without command arguments.' }, args.explicit === undefined ? 3 : 2);
    return;
  }
  value.executable = found;
  if (args.checkOnly) { emit({ ...value, status: 'ready' }); return; }
  try {
    const child = spawn(found.path, [], { cwd: path.dirname(found.path), shell: false, windowsHide: false, detached: true, stdio: 'ignore' });
    child.once('error', error => emit({ ...value, status: 'launch_failed', error: error.message }, 4));
    child.once('spawn', () => {
      child.unref();
      emit({ ...value, status: 'launch_requested', launched: true, process_id: child.pid,
        instructions: 'A Harness launch was requested. UI readiness, workspace and the Tianshu preset still need to be checked in the application.' });
    });
  } catch (error) { emit({ ...value, status: 'launch_failed', error: error.message }, 4); }
}
if (require.main === module) main(process.argv.slice(2));
module.exports = { main, argumentsOf, executableInfo, displayIconPath, registryCandidates, shortcutCandidates, findHarness };
