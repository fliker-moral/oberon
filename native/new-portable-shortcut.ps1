param([Parameter(Mandatory=$true)][string]$Destination)

$ErrorActionPreference = 'Stop'
$folder = (Resolve-Path -LiteralPath $Destination).Path
$target = '.\app\Oberon.exe'
if (-not (Test-Path -LiteralPath (Join-Path $folder $target) -PathType Leaf)) {
    throw 'Oberon.exe is missing from the build.'
}

$root = [IO.Path]::GetPathRoot($folder)
$unusedFolder = Join-Path $root ('Oberon-Package-' + [guid]::NewGuid().ToString('N'))
$template = Join-Path $folder ('.shortcut-' + [IO.Path]::GetRandomFileName() + '.lnk')
try {
    # Ask Windows for a target ID list rooted at a synthetic location that cannot point to the build.
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($template)
    $shortcut.TargetPath = Join-Path $unusedFolder 'app\Oberon.exe'
    $shortcut.Save()
    $raw = [IO.File]::ReadAllBytes($template)
} finally {
    if (Test-Path -LiteralPath $template) { Remove-Item -LiteralPath $template }
}

# MS-SHLLINK. Keep the native header and target ID list and replace the remaining data
# with an archive-relative path. Disable volume lookup and distributed link tracking.
if ($raw.Length -lt 80 -or [BitConverter]::ToUInt32($raw, 0) -ne 76 -or
    -not ([BitConverter]::ToUInt32($raw, 20) -band 1)) {
    throw 'Windows did not provide a target ID list for the shortcut.'
}
$idListEnd = 78 + [BitConverter]::ToUInt16($raw, 76)
if ($idListEnd -gt $raw.Length) { throw 'The shortcut target ID list is incomplete.' }
[BitConverter]::GetBytes([uint32]0x40189).CopyTo($raw, 20)
$linkPath = Join-Path $folder 'Oberon.lnk'
$writer = [IO.BinaryWriter]::new([IO.File]::Create($linkPath))
try {
    $writer.Write($raw, 0, $idListEnd)
    $writer.Write([uint16]$target.Length)
    $writer.Write([Text.Encoding]::Unicode.GetBytes($target))
    $writer.Write([uint32]0) # TerminalBlock.
} finally {
    $writer.Dispose()
}
