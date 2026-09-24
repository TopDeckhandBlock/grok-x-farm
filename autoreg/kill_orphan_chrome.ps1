# Убивает только chrome с debugging-портом (DrissionPage/patchright оркестровка).
# Обычный Chrome пользователя не трогает (у него нет remote-debugging-port).
Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" | Where-Object {
    $_.CommandLine -match 'remote-debugging-port'
} | ForEach-Object {
    $cmd = $_.CommandLine
    if ($cmd.Length -gt 140) { $cmd = $cmd.Substring(0, 140) }
    Write-Output ("KILL {0} :: {1}" -f $_.ProcessId, $cmd)
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}
