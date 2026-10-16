# Date: 2026-10-16. Maintainer: yangshaopeng. No model service or API key required.
$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
$backendDir = Join-Path $projectDir 'dpf_agent'
$pythonPath = Join-Path $backendDir '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw 'Create dpf_agent/.venv and install requirements-windows-py313.lock.txt first; see README.md.'
}
$previous = @{}
$values = @{
    DPF_ENGINE_BACKEND='com'; DPF_COM_ISOLATION='process'; DPF_LLM_ENABLED='0';
    DPF_RAG_ENABLED='0'; DPF_POWER_RAG_ENABLED='0'
}
try {
    foreach ($name in $values.Keys) {
        $previous[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
        [Environment]::SetEnvironmentVariable($name, $values[$name], 'Process')
    }
    Push-Location -LiteralPath $backendDir
    try {
        & $pythonPath -m dpf_agent --llm off run --dss case4 -m 'Compute case4 power flow'
        $demoExitCode = $LASTEXITCODE
    } finally { Pop-Location }
} finally {
    foreach ($name in $values.Keys) { [Environment]::SetEnvironmentVariable($name, $previous[$name], 'Process') }
}
exit $demoExitCode
