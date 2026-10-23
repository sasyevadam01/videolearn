<#
  VideoLearn - Windows installer (per-user, no admin rights needed).

  Easiest:   double-click "Install VideoLearn.bat"
  One line:  irm https://raw.githubusercontent.com/sasyevadam01/videolearn/main/install.ps1 | iex

  What it does: installs the fast Python manager "uv" (only if missing), a private Python 3.12 + the app
  in %LOCALAPPDATA%\VideoLearn, ffmpeg (only if missing), the NVIDIA CUDA libraries (only if an
  NVIDIA GPU is present), downloads the speech model once, and creates Desktop + Start menu shortcuts.
  On a PC with an AMD or Intel graphics card it can also set up the faster whisper.cpp + Vulkan engine
  (see -WhisperCppDir). Nothing outside that folder, your shortcuts and ~/.cache/huggingface (the model) is touched.

  Options (when running the file directly):
    -InstallDir <path>   where to install (default %LOCALAPPDATA%\VideoLearn)
    -Source <folder|zip> install from a local folder or a zip URL instead of GitHub
    -NoGpu               do not install the NVIDIA CUDA libraries
    -NoVulkan            do not set up the Vulkan engine (AMD / Intel graphics cards)
    -WhisperCppDir <dir> folder with a Vulkan build of whisper-cli.exe (and its DLLs, optionally models\);
                         it is copied to <install folder>\whispercpp. Only used on AMD / Intel PCs.
    -VulkanModel full|q5 ggml model for the Vulkan engine: full = large-v3-turbo (1.6 GB, default) or
                         q5 = quantized (0.6 GB, a little less accurate)
    -NoMcp               do not install the Claude connector (MCP server)
    -SkipModel           do not pre-download the speech model
    -NoShortcuts         do not create Desktop / Start menu shortcuts
#>
[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'VideoLearn'),
    [string]$Source = '',
    [switch]$NoGpu,
    [switch]$NoVulkan,
    [string]$WhisperCppDir = '',
    [ValidateSet('full', 'q5')][string]$VulkanModel = 'full',
    [switch]$NoMcp,
    [switch]$SkipModel,
    [switch]$NoShortcuts
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'      # Invoke-WebRequest is ~10x faster without the progress bar
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
# Antivirus / company proxies re-sign HTTPS traffic with a certificate that only Windows trusts: make uv use the Windows store.
$env:UV_SYSTEM_CERTS = '1'
$env:UV_NATIVE_TLS = '1'
$Repo = 'sasyevadam01/videolearn'
$Total = 7

function Step([int]$n, [string]$msg) { Write-Host ''; Write-Host "[$n/$Total] $msg" -ForegroundColor Cyan }
function Info([string]$msg) { Write-Host "      $msg" -ForegroundColor DarkGray }
function Download([string]$url, [string]$out) { Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $out }
# Native tools (uv, python) print progress on stderr; do not let that abort the script. Check $LASTEXITCODE instead.
function Native([scriptblock]$cmd) {
    $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { & $cmd 2>&1 | ForEach-Object { Write-Host "      $_" -ForegroundColor DarkGray } } finally { $ErrorActionPreference = $old }
}

if ($env:OS -ne 'Windows_NT') { throw 'This installer is for Windows. On other systems: pip install "videolearn[mcp]" (see README).' }

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$tmp = Join-Path ([IO.Path]::GetTempPath()) ('vl4c-' + [Guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
try { Start-Transcript -Path (Join-Path $InstallDir 'install.log') -Append | Out-Null } catch { }

try {
    Write-Host ''
    Write-Host '  VideoLearn - installer' -ForegroundColor White
    Write-Host "  Install folder: $InstallDir" -ForegroundColor DarkGray
    if ($InstallDir.Length -gt 100) { Write-Warning 'That install path is very long; Windows tools can misbehave above 260 characters. A shorter -InstallDir is safer.' }
    $free = (Get-PSDrive -Name ((Split-Path $InstallDir -Qualifier).TrimEnd(':'))).Free
    if ($free -lt 5GB) { Write-Warning ('Only {0:N1} GB free on that drive; the install needs about 5 GB (model + libraries), and 1.6 GB more for the Vulkan engine on AMD / Intel PCs.' -f ($free / 1GB)) }

    # 1 ---------------------------------------------------------------- uv
    Step 1 'Python manager (uv)'
    $uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
    if (-not $uv) {
        $uvDir = Join-Path $InstallDir 'tools'
        if (-not (Test-Path (Join-Path $uvDir 'uv.exe'))) {
            Info 'installing uv (a single small executable, only inside the install folder)'
            $env:UV_UNMANAGED_INSTALL = $uvDir
            Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
        }
        $uv = Join-Path $uvDir 'uv.exe'
    }
    if (-not (Test-Path $uv)) { throw 'Could not install uv.' }
    Info "using $uv"

    # 2 ------------------------------------------------------------ source
    Step 2 'Getting the app'
    $srcCopy = Join-Path $tmp 'src'
    if ($Source -and (Test-Path -PathType Container $Source)) { $local = (Resolve-Path $Source).Path }
    elseif (-not $Source -and $PSScriptRoot -and (Test-Path (Join-Path $PSScriptRoot 'pyproject.toml'))) { $local = $PSScriptRoot }
    else { $local = $null }
    if ($local) {
        Info "from $local"
        & robocopy $local $srcCopy /E /XD .git .venv cuda_libs build dist __pycache__ /XF *.mp4 /NFL /NDL /NJH /NJS /NP | Out-Null
    } else {
        $zipUrl = if ($Source) { $Source } else { "https://github.com/$Repo/archive/refs/heads/main.zip" }
        Info "downloading $zipUrl"
        $zip = Join-Path $tmp 'src.zip'
        Download $zipUrl $zip
        Expand-Archive -Path $zip -DestinationPath $tmp -Force
        $inner = Get-ChildItem $tmp -Directory | Where-Object { Test-Path (Join-Path $_.FullName 'pyproject.toml') } | Select-Object -First 1
        if (-not $inner) { throw 'The downloaded archive does not contain the app.' }
        Move-Item $inner.FullName $srcCopy
    }

    # 3 ------------------------------------------------------ python + app
    Step 3 'Installing Python and the app (fast)'
    $venv = Join-Path $InstallDir 'venv'
    $ok = $false
    for ($try = 1; $try -le 3 -and -not $ok; $try++) {      # antivirus scans can make the first attempt fail transiently
        Native { & $uv venv $venv --python 3.12 --allow-existing --quiet }
        if ($LASTEXITCODE -eq 0) { $ok = $true; break }
        Info "retrying ($try/3)..."
        # self-heal: a half-made "minor version" junction left by an interrupted/blocked first attempt
        $pyRoot = Join-Path $env:APPDATA 'uv\python'
        Get-ChildItem $pyRoot -Directory -Filter 'cpython-3.12-*' -ErrorAction SilentlyContinue |
            Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint } |
            ForEach-Object { cmd /c "rmdir `"$($_.FullName)`"" | Out-Null }
        Start-Sleep -Seconds 4
    }
    if (-not $ok) { throw 'uv could not create the Python environment.' }
    $py = Join-Path $venv 'Scripts\python.exe'
    $extras = if ($NoMcp) { '' } else { '[mcp]' }
    $uri = ([Uri]$srcCopy).AbsoluteUri
    Native { & $uv pip install --python $py --reinstall-package videolearn "videolearn$extras @ $uri" }
    if ($LASTEXITCODE -ne 0) { throw 'Installing the app failed.' }

    # 4 --------------------------------------------------------------- ffmpeg
    Step 4 'ffmpeg (video decoding)'
    $haveFfmpeg = (Get-Command ffmpeg -ErrorAction SilentlyContinue) -and (Get-Command ffprobe -ErrorAction SilentlyContinue)
    $ffBin = Join-Path $InstallDir 'ffmpeg\bin'
    if ($haveFfmpeg) { Info 'already on this PC, nothing to do' }
    elseif (Test-Path (Join-Path $ffBin 'ffmpeg.exe')) { Info 'already installed' }
    else {
        $urls = @('https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip',
                  'https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-win64-gpl.zip')
        $zip = Join-Path $tmp 'ffmpeg.zip'; $ok = $false
        foreach ($u in $urls) {
            try { Info "downloading $u"; Download $u $zip; $ok = $true; break } catch { Info 'failed, trying the next mirror' }
        }
        if (-not $ok) { throw 'Could not download ffmpeg. Install it from https://ffmpeg.org and run this installer again.' }
        $x = Join-Path $tmp 'ffx'; Expand-Archive -Path $zip -DestinationPath $x -Force
        $bin = Get-ChildItem $x -Recurse -Filter ffmpeg.exe | Select-Object -First 1
        New-Item -ItemType Directory -Force -Path $ffBin | Out-Null
        Copy-Item (Join-Path $bin.DirectoryName '*.exe') $ffBin -Force
    }

    # 5 ------------------------------------------------------------------ GPU
    Step 5 'Graphics card acceleration'
    $nv = (Get-Command nvidia-smi -ErrorAction SilentlyContinue) -or (Test-Path (Join-Path $env:SystemRoot 'System32\nvidia-smi.exe'))
    if ($NoGpu) { Info 'skipped (-NoGpu)' }
    elseif (-not $nv) {
        $gpuNames = @(Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue | ForEach-Object { $_.Name })
        $amdIntel = @($gpuNames | Where-Object { $_ -match 'AMD|Radeon|Intel' -and $_ -notmatch 'Remote Display|Basic' })
        if ($NoVulkan -or -not $amdIntel) {
            Info 'no NVIDIA graphics card found: the processor will be used (works fine, just slower)'
        }
        elseif (-not $WhisperCppDir -or -not (Test-Path (Join-Path $WhisperCppDir 'whisper-cli.exe'))) {
            Info "graphics card: $($amdIntel[0]). The faster Vulkan engine needs a whisper-cli.exe build (-WhisperCppDir), which was not given:"
            Info 'the processor will be used (works fine, just slower)'
        }
        else {
            Info "graphics card: $($amdIntel[0]): setting up the Vulkan engine (whisper.cpp)"
            $wc = Join-Path $InstallDir 'whispercpp'
            New-Item -ItemType Directory -Force -Path (Join-Path $wc 'models') | Out-Null
            Get-ChildItem -LiteralPath $WhisperCppDir -File | Copy-Item -Destination $wc -Force
            $gg = if ($VulkanModel -eq 'q5') { 'ggml-large-v3-turbo-q5_0.bin' } else { 'ggml-large-v3-turbo.bin' }
            $ggPath = Join-Path $wc "models\$gg"
            $given = Join-Path $WhisperCppDir "models\$gg"
            if (Test-Path $given) { Copy-Item $given $ggPath -Force }
            if (-not (Test-Path $ggPath)) {
                if ($SkipModel) { Info "skipped the Vulkan model (-SkipModel): copy $gg to $wc\models to use the Vulkan engine" }
                else {
                    $mb = if ($VulkanModel -eq 'q5') { 574 } else { 1625 }
                    Info "downloading the Vulkan speech model $gg (about $mb MB, one time)"
                    try { Download "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/$gg" "$ggPath.part"; Move-Item "$ggPath.part" $ggPath -Force }
                    catch { Remove-Item "$ggPath.part" -Force -ErrorAction SilentlyContinue
                            Write-Warning 'The Vulkan model could not be downloaded; the processor will be used.' }
                }
            }
            # the app checks that Vulkan really runs on the GPU the first time; if not, it uses the processor
        }
    }
    else {
        Info 'NVIDIA card found: installing the CUDA libraries (about 2 GB, one time)'
        Native { & $uv pip install --python $py nvidia-cublas-cu12 nvidia-cudnn-cu12 }
        if ($LASTEXITCODE -ne 0) { Write-Warning 'The CUDA libraries could not be installed; the app will use the processor.' }
    }

    # 6 ---------------------------------------------------------------- model
    Step 6 'Speech model'
    if ($SkipModel) { Info 'skipped (-SkipModel): it will download on first use' }
    else {
        Native { & $py -m videolearn.prefetch }
        if ($LASTEXITCODE -ne 0) { Write-Warning 'The model could not be downloaded now; it will download on first use.' }
    }

    # 7 ------------------------------------------------------------ shortcuts
    Step 7 'Shortcuts'
    $ico = (& $py -c "import videolearn, os; print(os.path.join(os.path.dirname(videolearn.__file__), 'ui', 'icon.ico'))").Trim()
    Copy-Item $ico (Join-Path $InstallDir 'icon.ico') -Force
    $uninst = if ($local) { Join-Path $local 'uninstall.ps1' } else { Join-Path $srcCopy 'uninstall.ps1' }
    if (Test-Path $uninst) { Copy-Item $uninst (Join-Path $InstallDir 'uninstall.ps1') -Force }
    if ($NoShortcuts) { Info 'skipped (-NoShortcuts)' }
    else {
        $ws = New-Object -ComObject WScript.Shell
        $targets = @([Environment]::GetFolderPath('Desktop'), (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'))
        foreach ($dir in $targets) {
            $lnk = $ws.CreateShortcut((Join-Path $dir 'VideoLearn.lnk'))
            $lnk.TargetPath = Join-Path $venv 'Scripts\pythonw.exe'
            $lnk.Arguments = '-m videolearn.app'
            $lnk.WorkingDirectory = $InstallDir
            $lnk.IconLocation = (Join-Path $InstallDir 'icon.ico') + ',0'
            $lnk.Description = 'VideoLearn - give Claude ears for your screen recordings'
            $lnk.Save()
        }
        Info 'created on the Desktop and in the Start menu'

        # The project used to be called "VideoLearn 4Claude": remove that older install and its icons (your glossary
        # and recent list are copied over by the app on first start).
        $legacyDir = Join-Path $env:LOCALAPPDATA 'VideoLearn4Claude'
        foreach ($d in $targets) { Remove-Item -LiteralPath (Join-Path $d 'VideoLearn 4Claude.lnk') -Force -ErrorAction SilentlyContinue }
        if ((Test-Path -LiteralPath $legacyDir) -and ($legacyDir -ne $InstallDir)) {
            Info 'removing the older "VideoLearn 4Claude" install'
            Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path.StartsWith($legacyDir, [StringComparison]::OrdinalIgnoreCase) } |
                Stop-Process -Force -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 1
            Remove-Item -LiteralPath $legacyDir -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    $mcpExe = Join-Path $venv 'Scripts\videolearn-mcp.exe'
    Write-Host ''
    Write-Host '  Done! Open "VideoLearn" from your Desktop or Start menu.' -ForegroundColor Green
    if (-not $NoMcp) {
        Write-Host ''
        Write-Host '  Optional - let Claude use it by itself (connector):' -ForegroundColor White
        Write-Host ('    Claude Code:    claude mcp add videolearn -- "' + $mcpExe + '"') -ForegroundColor Yellow
        Write-Host '    Claude Desktop: Settings > Developer > Edit Config, add under "mcpServers":'
        $json = ('"videolearn": { "command": "' + ($mcpExe -replace '\\', '\\') + '" }')
        Write-Host "      $json" -ForegroundColor Yellow
    }
}
catch {
    Write-Host ''
    Write-Host "  Installation failed: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "  Details: $(Join-Path $InstallDir 'install.log')" -ForegroundColor DarkGray
    $script:failed = $true
}
finally {
    try { Stop-Transcript | Out-Null } catch { }
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
}
if ($script:failed) { exit 1 }
