param(
    [Parameter(Mandatory=$true)][string]$Dependencies,
    [Parameter(Mandatory=$true)][string]$Destination,
    [Parameter(Mandatory=$true)][string]$IconPng
)

$ErrorActionPreference = 'Stop'
$source = Split-Path -Parent $PSScriptRoot
$python = Join-Path $Dependencies 'python'
$webview = Join-Path $Dependencies 'webview2'
$dest = New-Item -Path $Destination -ItemType Directory -Force
$app = New-Item -Path (Join-Path $dest.FullName 'app') -ItemType Directory -Force
$runtime = New-Item -Path (Join-Path $app.FullName 'runtime') -ItemType Directory -Force
Copy-Item -Path (Join-Path $python '*') -Destination $runtime.FullName

# The ._pth in the embeddable runtime includes the directory containing its executable.
foreach ($name in @('oberon', 'static')) {
    $target = New-Item -Path (Join-Path $runtime.FullName $name) -ItemType Directory -Force
    Get-ChildItem -LiteralPath (Join-Path $source $name) -File | Copy-Item -Destination $target.FullName
}
Copy-Item -LiteralPath (Join-Path $source 'catalog.json') -Destination $runtime.FullName
Copy-Item -LiteralPath (Join-Path $source 'Начало работы.txt') -Destination $dest.FullName

$core = Join-Path $webview 'lib\net462\Microsoft.Web.WebView2.Core.dll'
$forms = Join-Path $webview 'lib\net462\Microsoft.Web.WebView2.WinForms.dll'
Copy-Item -LiteralPath $core,$forms,(Join-Path $webview 'runtimes\win-x64\native\WebView2Loader.dll') -Destination $app.FullName
Copy-Item -LiteralPath (Join-Path $webview 'LICENSE.txt') -Destination (Join-Path $app.FullName 'LICENSE-WebView2.txt')
Copy-Item -LiteralPath (Join-Path $webview 'NOTICE.txt') -Destination (Join-Path $app.FullName 'NOTICE-WebView2.txt')

# Convert the generated PNG to a Windows icon with sizes for Explorer and the taskbar.
try { Add-Type -AssemblyName System.Drawing.Common -ErrorAction Stop }
catch { Add-Type -AssemblyName System.Drawing }
$original = [System.Drawing.Image]::FromFile($IconPng)
$sizes = @(16,24,32,48,64,128,256)
$frames = [System.Collections.Generic.List[byte[]]]::new()
try {
    foreach ($size in $sizes) {
        $bitmap = [System.Drawing.Bitmap]::new($size, $size, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
        $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
        $buffer = [System.IO.MemoryStream]::new()
        try {
            $graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
            $graphics.DrawImage($original, 0, 0, $size, $size)
            $bitmap.Save($buffer, [System.Drawing.Imaging.ImageFormat]::Png)
            $frames.Add($buffer.ToArray())
            if ($size -eq 128) {
                $bitmap.Save((Join-Path $runtime.FullName 'static\oberon.png'), [System.Drawing.Imaging.ImageFormat]::Png)
            }
        } finally {
            $buffer.Dispose()
            $graphics.Dispose()
            $bitmap.Dispose()
        }
    }
} finally {
    $original.Dispose()
}
$ico = Join-Path $app.FullName 'Oberon.ico'
$writer = [System.IO.BinaryWriter]::new([System.IO.File]::Create($ico))
try {
    $writer.Write([uint16]0)
    $writer.Write([uint16]1)
    $writer.Write([uint16]$sizes.Count)
    $offset = 6 + 16 * $sizes.Count
    for ($i=0; $i -lt $sizes.Count; $i++) {
        $side = [byte]($sizes[$i] % 256)
        $writer.Write($side)
        $writer.Write($side)
        $writer.Write([uint16]0)
        $writer.Write([uint16]1)
        $writer.Write([uint16]32)
        $writer.Write([uint32]$frames[$i].Length)
        $writer.Write([uint32]$offset)
        $offset += $frames[$i].Length
    }
    foreach ($frame in $frames) { $writer.Write($frame) }
} finally {
    $writer.Dispose()
}

$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$exe = Join-Path $app.FullName 'Oberon.exe'
$cscArgs = @(
    '/nologo', '/utf8output', '/codepage:65001', '/target:winexe', '/platform:x64', '/optimize+',
    "/out:$exe", "/win32icon:$ico", "/win32manifest:$PSScriptRoot\Oberon.manifest",
    '/reference:System.Windows.Forms.dll', '/reference:System.Drawing.dll',
    "/reference:$core", "/reference:$forms", "$PSScriptRoot\Oberon.cs"
)
& $compiler @cscArgs
if ($LASTEXITCODE -ne 0) { throw 'Сборка Oberon.exe не удалась.' }
& (Join-Path $PSScriptRoot 'new-portable-shortcut.ps1') -Destination $dest.FullName
Get-Item -LiteralPath $exe
