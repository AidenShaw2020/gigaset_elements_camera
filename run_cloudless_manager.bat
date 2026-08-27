@echo off
setlocal
cd /d "%~dp0"
echo Gigaset Gen1 Camera Cloudless Manager
echo.
set /p CAMERA_ADDRESS=Camera IP address:
set /p CAMERA_MAC_ADDRESS=Camera MAC address:
echo.
py -3 cloudless_manager.py --camera "%CAMERA_ADDRESS%" --mac "%CAMERA_MAC_ADDRESS%"
if errorlevel 1 (
  echo.
  echo The manager stopped with an error.
  pause
)
endlocal
