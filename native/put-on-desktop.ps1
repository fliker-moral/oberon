param([Parameter(Mandatory=$true)][string]$BuiltFolder)

$ErrorActionPreference = 'Stop'
$build = (Resolve-Path -LiteralPath $BuiltFolder).Path
$desktop = [Environment]::GetFolderPath('DesktopDirectory')
if ([string]::IsNullOrWhiteSpace($desktop) -or -not (Test-Path -LiteralPath $desktop -PathType Container)) {
    throw 'Папка рабочего стола не найдена.'
}
$destination = Join-Path $desktop 'Oberon'
$buildExecutable = Join-Path $build 'app\Oberon.exe'
$buildIcon = Join-Path $build 'app\Oberon.ico'
if (-not (Test-Path -LiteralPath $buildExecutable) -or -not (Test-Path -LiteralPath $buildIcon)) {
    throw 'В папке сборки нет Oberon.exe или Oberon.ico.'
}
if (Test-Path -LiteralPath $destination) {
    $existing = Get-Item -LiteralPath $destination -Force
    if (-not $existing.PSIsContainer -or ($existing.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'Имя Oberon на рабочем столе занято файлом или ссылкой.'
    }
    foreach ($name in @('app', 'Oberon.lnk', 'Начало работы.txt')) {
        if (Test-Path -LiteralPath (Join-Path $destination $name)) {
            throw "В папке уже есть $name. Сохраните предыдущую версию отдельно."
        }
    }
} else {
    New-Item -ItemType Directory -Path $destination | Out-Null
}
Copy-Item -LiteralPath (Join-Path $build 'app') -Destination $destination -Recurse
Copy-Item -LiteralPath (Join-Path $build 'Начало работы.txt') -Destination $destination

$exe = Join-Path $destination 'app\Oberon.exe'
$icon = Join-Path $destination 'app\Oberon.ico'
$shortcutPath = Join-Path $destination 'Oberon.lnk'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $exe
$shortcut.WorkingDirectory = Split-Path -Parent $exe
$shortcut.IconLocation = "$icon,0"
$shortcut.Description = 'Oberon — проекты, Git и анализ'
$shortcut.Save()

$saved = $shell.CreateShortcut($shortcutPath)
if ($saved.TargetPath -ne $exe -or -not $saved.IconLocation.StartsWith($icon)) {
    throw 'Не удалось подтвердить привязку ярлыка к программе и иконке.'
}
if ((Get-FileHash -LiteralPath $exe).Hash -ne (Get-FileHash -LiteralPath $buildExecutable).Hash) {
    throw 'Копия Oberon.exe отличается от проверенной сборки.'
}
[pscustomobject]@{
    Shortcut = $shortcutPath
    Program = $saved.TargetPath
    Icon = $saved.IconLocation
    Version = (Get-Item -LiteralPath $exe).VersionInfo.FileVersion
}
