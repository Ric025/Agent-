# 检查这台电脑的环境，装好运行要用的 Python 和依赖（Windows）。和 setup/install.sh 做的事一样。
#
# 用法（一般不用手工运行，start.bat 会调用）：
#   powershell -NoProfile -ExecutionPolicy Bypass -File setup\install.ps1            检查并安装（装好了就跳过）
#   ... -File setup\install.ps1 -Force      重新检查、重新安装
#   ... -File setup\install.ps1 -IfNeeded   给 start.bat 用：装好了就安静地退出；上次失败不到一天不再重试
#   ... -File setup\install.ps1 -Check      只做环境检查
#
# 只动项目文件夹里的 .venv\ 和 .tools\，不改系统的 Python，不改环境变量，不需要管理员权限。
# 过程记在 setup\安装记录.txt，检查结果写在 setup\环境检查结果.txt。
# 公司的软件源、代理、离线安装：见 setup\mirror.env.example 和 README。
# 兼容 Windows 10 自带的 PowerShell 5.1（这个文件要存成带 BOM 的 UTF-8，否则中文会乱）。
param([switch]$Force, [switch]$IfNeeded, [switch]$Check)

$ErrorActionPreference = 'Continue'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch { }

$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $Here
Set-Location $Root
$PyWant = '3.12'
$Venv = Join-Path $Root '.venv'
$VPy = Join-Path $Venv 'Scripts\python.exe'
$Tools = Join-Path $Root '.tools'
$State = Join-Path $Tools 'setup-state.json'
$LogFile = Join-Path $Here '安装记录.txt'
$Wheels = Join-Path $Here 'wheels'
$UvInstallerUrl = 'https://astral.sh/uv/install.ps1'
New-Item -ItemType Directory -Force -Path $Tools | Out-Null

function LogOnly([string]$t) { Add-Content -Path $LogFile -Value $t -Encoding UTF8 }
function Say([string]$t) { Write-Host $t; LogOnly $t }
function Step([string]$n, [string]$t) { Write-Host ''; Write-Host "【$n】$t"; LogOnly ''; LogOnly "== $n $t  $(Get-Date -Format 'HH:mm:ss') ==" }
# 跑一个程序，输出只进记录；返回退出码
function Run([string]$exe, [string[]]$argv) {
  LogOnly ("> " + $exe + " " + ($argv -join ' '))
  & $exe @argv 2>&1 | ForEach-Object { "$_" } | Out-File -FilePath $LogFile -Append -Encoding UTF8
  return $LASTEXITCODE
}
function PyOk([string]$exe, [int]$maj, [int]$min) {
  if (-not $exe) { return $false }
  & $exe -c "import sys; sys.exit(0 if sys.version_info >= ($maj, $min) else 1)" 2>$null | Out-Null
  return ($LASTEXITCODE -eq 0)
}
function PyVer([string]$exe) { (& $exe -c "import platform; print(platform.python_version())" 2>$null) }
function JsonEsc([string]$s) { return ($s -replace '\\', '\\' -replace '"', '\"') }

function HashReqs {
  $t = (Get-Content (Join-Path $Here 'requirements-core.txt') -Raw) + (Get-Content (Join-Path $Here 'requirements-extra.txt') -Raw) + "python=$PyWant`n"
  $sha = [System.Security.Cryptography.SHA256]::Create()
  return -join ($sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($t)) | ForEach-Object { $_.ToString('x2') })
}
$ReqHash = HashReqs

function WriteState([string]$ok, [string]$why, [string]$pyv, [string]$extraFailed) {
  $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
  $line = '{"req": "' + $ReqHash + '", "ok": ' + $ok + ', "why": "' + (JsonEsc $why) + '", "python": "' + $pyv + '", "extra_failed": "' + (JsonEsc $extraFailed) + '", "at": ' + $now + '}'
  [System.IO.File]::WriteAllText($State, $line + "`n", (New-Object System.Text.UTF8Encoding($false)))
}
function ReadStateText { if (Test-Path $State) { return (Get-Content $State -Raw) } else { return '' } }

# 能用的 Python 3.11+：py 启动器、PATH 上的 python、常见的安装位置。跳过 WindowsApps 里那个会打开应用商店的假 python
function FindPython {
  $c = @()
  if (Get-Command py -ErrorAction SilentlyContinue) {
    foreach ($v in @('3.12', '3.11', '3.13')) {
      $p = (& py "-$v" -c "import sys; print(sys.executable)" 2>$null)
      if ($LASTEXITCODE -eq 0 -and $p) { $c += $p.Trim() }
    }
  }
  foreach ($n in @('python', 'python3')) {
    $cmd = Get-Command $n -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -notmatch '\\WindowsApps\\') { $c += $cmd.Source }
  }
  foreach ($v in @('312', '311', '313')) {
    $c += (Join-Path $env:LOCALAPPDATA "Programs\Python\Python$v\python.exe")
    $c += (Join-Path $env:ProgramFiles "Python$v\python.exe")
  }
  foreach ($p in $c) { if ($p -and (Test-Path $p) -and (PyOk $p 3 11)) { return $p } }
  return $null
}
function FindAnyPython {
  if (Get-Command py -ErrorAction SilentlyContinue) {
    $p = (& py -3 -c "import sys; print(sys.executable)" 2>$null)
    if ($LASTEXITCODE -eq 0 -and $p -and (PyOk $p.Trim() 3 8)) { return $p.Trim() }
  }
  $cmd = Get-Command python -ErrorAction SilentlyContinue
  if ($cmd -and $cmd.Source -notmatch '\\WindowsApps\\' -and (PyOk $cmd.Source 3 8)) { return $cmd.Source }
  return $null
}

# setup\mirror.env：只认几个键，不执行它
function LoadMirror {
  $f = Join-Path $Here 'mirror.env'
  if (-not (Test-Path $f)) { return }
  foreach ($line in (Get-Content $f -Encoding UTF8)) {
    $l = $line.Trim()
    if ($l -eq '' -or $l.StartsWith('#') -or -not $l.Contains('=')) { continue }
    $k = $l.Substring(0, $l.IndexOf('=')).Trim(); $v = $l.Substring($l.IndexOf('=') + 1).Trim().Trim('"')
    if ($v -eq '') { continue }
    switch ($k) {
      'PYPI_MIRROR' { $env:PIP_INDEX_URL = $v; $env:UV_DEFAULT_INDEX = $v; $env:UV_INDEX_URL = $v }
      'PIP_TRUSTED_HOST' { $env:PIP_TRUSTED_HOST = $v }
      'PYTHON_MIRROR' { $env:UV_PYTHON_INSTALL_MIRROR = $v }
      'UV_INSTALLER_URL' { $script:UvInstallerUrl = $v }
      'HTTPS_PROXY' { $env:HTTPS_PROXY = $v }
      'HTTP_PROXY' { $env:HTTP_PROXY = $v }
      'NO_PROXY' { $env:NO_PROXY = $v }
      'SYSTEM_CERTS' { if ($v -eq '1') { $env:UV_SYSTEM_CERTS = '1'; $env:UV_NATIVE_TLS = '1' } }
      default { LogOnly "mirror.env 里不认识的键：$k（忽略）" }
    }
  }
  LogOnly '已读 setup\mirror.env'
}

function InstalledOk {
  if (-not (Test-Path $VPy) -or -not (PyOk $VPy 3 11)) { return $false }
  $s = ReadStateText
  return ($s.Contains('"req": "' + $ReqHash + '"') -and $s.Contains('"ok": true'))
}

# ---------------- 先看要不要装 ----------------

if ($Check) {
  $p = $null
  if ((Test-Path $VPy) -and (PyOk $VPy 3 8)) { $p = $VPy } else { $p = FindPython; if (-not $p) { $p = FindAnyPython } }
  if (-not $p) { Write-Host '这台电脑上没有能用的 Python，先运行 start.bat 自动安装。'; exit 2 }
  & $p (Join-Path $Here 'check_env.py') --write
  exit $LASTEXITCODE
}

if (-not $Force -and (InstalledOk)) {
  if (-not $IfNeeded) { Write-Host "已经装好了（$(PyVer $VPy)，依赖清单没有变化）。要重装：start.bat --setup" }
  exit 0
}
if ($IfNeeded) {
  $s = ReadStateText
  if ($s.Contains('"req": "' + $ReqHash + '"') -and $s.Contains('"ok": false')) {
    $m = [regex]::Match($s, '"at": (\d+)')
    $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    if ($m.Success -and ($now - [int]$m.Groups[1].Value) -lt 86400) {
      $w = [regex]::Match($s, '"why": "([^"]*)"').Groups[1].Value
      Write-Host "上次自动安装没有成功：$w"
      Write-Host '今天先不再重试（详细记录在 setup\安装记录.txt）。修好以后重试：start.bat --setup'
      exit 3
    }
  }
}

Set-Content -Path $LogFile -Value "安装记录  $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  Windows  项目：$Root" -Encoding UTF8
Write-Host '============================================================'
Write-Host ' 检查这台电脑的环境，装好需要的东西（第一次运行，或者依赖有变化）。'
Write-Host ' 只装在项目文件夹的 .venv 和 .tools 里，不改系统，不要管理员权限。'
Write-Host ' 第一次大约 3 到 10 分钟，看网速。以后启动不会再装。'
Write-Host '============================================================'
LoadMirror
$Uv = $null

function Fail([string]$why, [string]$fix) {
  WriteState 'false' $why '' ''
  Write-Host ''
  Say "没有装好：$why"
  if ($fix) { Say $fix }
  Say '详细记录：setup\安装记录.txt'
  exit 1
}

# ---------------- 1. 电脑 ----------------
Step '1/5' '检查电脑'
try { $os = Get-CimInstance Win32_OperatingSystem; Say "  系统：$($os.Caption) $($os.Version)（$env:PROCESSOR_ARCHITECTURE）" } catch { Say "  系统：Windows（$env:PROCESSOR_ARCHITECTURE）" }
try { $mem = (Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory; Say ("  内存：{0:N0} GB" -f ($mem / 1GB)) } catch { }
try {
  $drive = Get-PSDrive -Name $Root.Substring(0, 1) -ErrorAction Stop
  Say ("  项目所在的磁盘还剩：{0:N1} GB" -f ($drive.Free / 1GB))
  if ($drive.Free -lt 1.5GB) { Fail ("磁盘空间不够：还剩 {0:N1} GB，安装需要大约 1.5 GB。" -f ($drive.Free / 1GB)) '腾出一些空间后重新运行。' }
} catch { }

# ---------------- 2. Python ----------------
Step '2/5' '找 Python 3.11 或更新的版本'
$MPy = $null
if ($env:CA_SETUP_PYTHON) {   # 指定用哪个 Python（IT 统一部署时、自动测试时用）
  if (PyOk $env:CA_SETUP_PYTHON 3 11) { $MPy = $env:CA_SETUP_PYTHON } else { Say "  CA_SETUP_PYTHON=$($env:CA_SETUP_PYTHON) 不能用（要 3.11 以上），改为自动找。" }
}
if (-not $MPy) { $MPy = FindPython }
if ($MPy) {
  Say "  用电脑上已有的：$MPy（$(PyVer $MPy)）"
} else {
  $old = FindAnyPython
  if ($old) { Say "  电脑上只有 Python $(PyVer $old)，不够新。自动装一个 $PyWant（只装在项目的 .tools 里）。" }
  else { Say "  电脑上没有能用的 Python。自动装一个 $PyWant（只装在项目的 .tools 里）。" }
  foreach ($c in @((Join-Path $Tools 'uv\uv.exe'), (Join-Path $Tools 'uv\bin\uv.exe'), (Join-Path $Here 'bin\uv.exe'))) {
    if (Test-Path $c) { $Uv = $c; break }
  }
  if (-not $Uv) { $cmd = Get-Command uv -ErrorAction SilentlyContinue; if ($cmd) { $Uv = $cmd.Source } }
  if (-not $Uv) {
    Say '  下载安装工具 uv……'
    $inst = Join-Path $Tools 'uv-install.ps1'
    try {
      Invoke-WebRequest -UseBasicParsing -Uri $UvInstallerUrl -OutFile $inst -TimeoutSec 120
      $env:UV_UNMANAGED_INSTALL = (Join-Path $Tools 'uv')
      $env:UV_NO_MODIFY_PATH = '1'
      Run 'powershell' @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $inst) | Out-Null
    } catch { LogOnly "下载 uv 安装脚本失败：$_" }
    foreach ($c in @((Join-Path $Tools 'uv\uv.exe'), (Join-Path $Tools 'uv\bin\uv.exe'), (Join-Path $env:USERPROFILE '.local\bin\uv.exe'))) { if (Test-Path $c) { $Uv = $c; break } }
  }
  if (-not $Uv -and $old) {
    Say "  换个办法：用电脑上的 Python $(PyVer $old) 从软件源装 uv……"
    $uvenv = Join-Path $Tools 'uvenv'
    if ((Run $old @('-m', 'venv', $uvenv)) -eq 0 -and (Run (Join-Path $uvenv 'Scripts\python.exe') @('-m', 'pip', 'install', '--disable-pip-version-check', 'uv')) -eq 0) {
      $c = Join-Path $uvenv 'Scripts\uv.exe'; if (Test-Path $c) { $Uv = $c }
    }
  }
  if (-not $Uv) { Fail '装不上安装工具 uv（连不上 astral.sh / GitHub，也连不上 Python 软件源）。' '办法：从 https://www.python.org/downloads/ 装一个 Python 3.12（安装时勾选 Add python.exe to PATH）后重新运行；或者在 setup\mirror.env 里配公司的代理、软件源。' }
  Say "  uv：$Uv（$(& $Uv --version 2>$null)）"
  $env:UV_PYTHON_INSTALL_DIR = (Join-Path $Tools 'python')
  Say "  下载 Python $PyWant……"
  if ((Run $Uv @('python', 'install', $PyWant)) -ne 0) { Fail "下载 Python $PyWant 没成功（要能访问 GitHub，或者在 setup\mirror.env 里配 PYTHON_MIRROR）。" '办法：从 https://www.python.org/downloads/ 装一个 Python 3.12 后重新运行。' }
  $MPy = (& $Uv python find $PyWant 2>$null | Select-Object -Last 1)
  if (-not $MPy -or -not (PyOk $MPy.Trim() 3 11)) { Fail "装好的 Python 找不到或不能运行：$MPy" '' }
  $MPy = $MPy.Trim()
  Say "  装好了：$MPy（$(PyVer $MPy)）"
}

# ---------------- 3. 虚拟环境 ----------------
Step '3/5' '准备项目自己的 Python 环境（.venv）'
if ((Test-Path $Venv) -and -not ((Test-Path $VPy) -and (PyOk $VPy 3 11))) {
  $aside = "$Venv.old-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
  Rename-Item -Path $Venv -NewName (Split-Path -Leaf $aside)
  Say "  原来的 .venv 不能用了（Python 版本太旧或者坏了），改名留在 $(Split-Path -Leaf $aside)，可以删掉。"
}
if (-not (Test-Path $VPy)) {
  if ((Run $MPy @('-m', 'venv', $Venv)) -ne 0) { Fail '建虚拟环境没成功。' '' }
  Say "  建好了：.venv（$(PyVer $VPy)）"
} else { Say "  已有：.venv（$(PyVer $VPy)）" }
$PyV = PyVer $VPy

# ---------------- 4. 依赖 ----------------
Step '4/5' '安装依赖'
$Offline = @()
if ((Test-Path $Wheels) -and (Get-ChildItem $Wheels -Filter *.whl -ErrorAction SilentlyContinue)) {
  $Offline = @('--no-index', '--find-links', $Wheels)
  Say '  用 setup\wheels 里的安装包，不联网。'
} else {
  $idx = $env:PIP_INDEX_URL; if (-not $idx) { $idx = 'https://pypi.org/simple' }
  try {
    Invoke-WebRequest -UseBasicParsing -Method Head -Uri ($idx.TrimEnd('/') + '/pip/') -TimeoutSec 15 | Out-Null
    Say "  软件源：$idx（连得上）"
  } catch {
    Fail "连不上 Python 软件源 $idx。" '办法：检查网络；公司要走代理或用内部软件源的，把 setup\mirror.env.example 复制成 setup\mirror.env 填好后重新运行。'
  }
}
function PipInstall([string[]]$what) {
  if ($Uv) { return (Run $Uv (@('pip', 'install', '--python', $VPy) + $Offline + $what)) }
  return (Run $VPy (@('-m', 'pip', 'install', '--disable-pip-version-check') + $Offline + $what))
}
if (-not $Uv -and $Offline.Count -eq 0) { Run $VPy @('-m', 'pip', 'install', '--disable-pip-version-check', '--upgrade', 'pip') | Out-Null }
Say '  必需的（LangChain / LangGraph、Office 文档、PDF）……'
if ((PipInstall @('-r', (Join-Path $Here 'requirements-core.txt'))) -ne 0) { Fail '必需的依赖没装上。' '常见原因：网络断了、软件源里缺包。看 setup\安装记录.txt 最后几十行。' }
Say '  可选的（逐个装，装不上只少对应的功能）：'
$ExtraFailed = @()
foreach ($line in (Get-Content (Join-Path $Here 'requirements-extra.txt') -Encoding UTF8)) {
  # 去掉注释和 ; 后面的条件（.venv 一定是 3.11 以上，用不上；PowerShell 5.1 传带双引号的参数给 pip 会弄坏引号）
  $spec = (($line -replace '#.*$', '') -split ';')[0].Trim()
  if ($spec -eq '') { continue }
  $name = ($spec -split '[<>=!;~ ]')[0]
  if ((PipInstall @($spec)) -eq 0) { Say "    $name：装好了" } else { Say "    $name：没装上"; $ExtraFailed += $name }
}
WriteState 'true' '' $PyV ($ExtraFailed -join ' ')
# 操作网页用电脑上的 Edge（Windows 10、11 都自带），不用另外下载浏览器

# ---------------- 5. 检查 ----------------
Step '5/5' '检查装好的环境'
& $VPy (Join-Path $Here 'check_env.py') --write --smoke
Write-Host ''
if ($LASTEXITCODE -eq 0) { Say '装好了。以后直接双击 start.bat。' }
else { Say '装好了，但检查发现有问题，见上面和 setup\环境检查结果.txt。程序照样能启动，有问题的功能会说明原因。' }
exit 0
