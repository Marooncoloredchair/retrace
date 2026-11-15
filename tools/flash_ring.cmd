@echo off
rem Compile and flash xiao_sense_usb_motor (VGA jpeg_q 10, auto AE ae_level 0, gainceil 32x).
cd /d "%~dp0\.."
set FQBN=esp32:esp32:XIAO_ESP32S3:PSRAM=opi,CDCOnBoot=default,USBMode=hwcdc
set SKETCH=%cd%\firmware\xiao_sense_usb_motor\xiao_sense_usb_motor.ino
set PORT=%1
if "%PORT%"=="" set PORT=COM5
echo Compiling %SKETCH% ...
"C:\Program Files\Arduino CLI\arduino-cli.exe" compile --fqbn %FQBN% "%SKETCH%"
if errorlevel 1 exit /b 1
echo Uploading to %PORT% ...
"C:\Program Files\Arduino CLI\arduino-cli.exe" upload -p %PORT% --fqbn %FQBN% "%SKETCH%"
exit /b %ERRORLEVEL%
