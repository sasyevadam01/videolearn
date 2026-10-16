<#
  VideoLearn - uninstaller.
  Removes the app folder and the shortcuts. Your reports (next to your videos) are never touched.
    -RemoveUserData   also delete your glossary and recent-list (~/.videolearn)
    -RemoveModels     also delete the downloaded speech models from the Hugging Face cache
#>
[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'VideoLearn'),
    [switch]$RemoveUserData,
    [switch]$RemoveModels
)
$ErrorActionPreference = 'Continue'

foreach ($dir in @([Environment]::GetFolderPath('Desktop'), (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'))) {
    Remove-Item -LiteralPath (Join-Path $dir 'VideoLearn.lnk') -Force -ErrorAction SilentlyContinue
}
Remove-Item -LiteralPath $InstallDir -Recurse -Force -ErrorAction SilentlyContinue
if ($RemoveUserData) { Remove-Item -LiteralPath (Join-Path $HOME '.videolearn') -Recurse -Force -ErrorAction SilentlyContinue }
if ($RemoveModels) {
    Get-ChildItem (Join-Path $HOME '.cache\huggingface\hub') -Directory -Filter 'models--*faster-whisper*' -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
}
Write-Host 'VideoLearn has been removed.' -ForegroundColor Green
if (Test-Path -LiteralPath $InstallDir) { Write-Warning "Some files in $InstallDir are still in use; delete the folder after closing the app." }
