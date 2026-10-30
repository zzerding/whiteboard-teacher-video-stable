# whiteboard-teacher-video 安装（Windows PowerShell 薄入口）
#   powershell -ExecutionPolicy Bypass -File scripts/setup.ps1 [--check|--force]
# 全部逻辑在 scripts/setup.py：这里只找 Python、原样转发参数、原样返回退出码，
# 不再复制一份引擎/依赖安装逻辑（复制过的两份曾经各修各的、行为漂移）。
param([switch]$Force)

$ErrorActionPreference = "Stop"
$Setup = Join-Path $PSScriptRoot "setup.py"

$Forward = @()
if ($Force) { $Forward += "--force" }
$Forward += $args

$Candidates = @()
if ($env:PYTHON) { $Candidates += , @($env:PYTHON) }
$Candidates += , @("py", "-3")
$Candidates += , @("python3")
$Candidates += , @("python")

foreach ($cand in $Candidates) {
    $exe = $cand[0]
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
    $extra = @()
    if ($cand.Count -gt 1) { $extra = @($cand[1..($cand.Count - 1)]) }
    & $exe @extra $Setup @Forward
    $code = $LASTEXITCODE
    # 9009 = Microsoft Store 的 python.exe 占位符（没真装 Python），换下一个候选
    if ($code -ne 9009) { exit $code }
}

Write-Host "✗ 找不到 Python（需要 3.9+）：https://www.python.org/downloads/" -ForegroundColor Red
exit 1