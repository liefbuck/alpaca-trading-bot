# Trading bot watchdog — restarts bot.py and serve.py if they die.
# Runs silently in the background. Add a shortcut to this in your Startup folder.
$py      = "C:\Users\liefb\AppData\Local\Python\pythoncore-3.14-64\python.exe"
$baseDir = "C:\Users\liefb\OneDrive\Documents\ClaudeTrading"
$log     = "$baseDir\watchdog.log"

function Write-Log($msg) {
    $ts = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    Add-Content $log "$ts  $msg"
}

Write-Log "Watchdog started"

while ($true) {
    foreach ($script in @("bot.py", "serve.py")) {
        $running = Get-WmiObject Win32_Process -Filter "name='python.exe'" |
                   Where-Object { $_.CommandLine -like "*$script*" }
        if (-not $running) {
            Write-Log "RESTART: $script not running — relaunching"
            Start-Process $py -ArgumentList $script -WorkingDirectory $baseDir -WindowStyle Minimized
        }
    }
    Start-Sleep -Seconds 60
}
