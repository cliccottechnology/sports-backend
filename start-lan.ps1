# JETZ backend — restart on LAN (phone/Expo accessible). Run from backend/: .\start-lan.ps1
$conn = netstat -ano | Select-String "LISTENING" | Select-String "0.0.0.0:8000|127.0.0.1:8000" | Select-Object -First 1
if ($conn) {
  $pid8000 = ($conn -split "\s+")[-1]
  Write-Output "Stopping old server (pid $pid8000)…"
  Stop-Process -Id $pid8000 -Force -ErrorAction SilentlyContinue
  Start-Sleep -Seconds 3
}
Write-Output "Starting Django on 0.0.0.0:8000 …"
py -3.14 manage.py runserver 0.0.0.0:8000
