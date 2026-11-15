@echo off
rem Start ReTrace for the ring. Extra words are passed on, for example:
rem   retrace --no-motor
rem   retrace weartest --minutes 10
rem   retrace vlm-test path\to\image.jpg
rem   retrace replay-holds reports\weartest_20261114_045012
rem   retrace replay-find reports\weartest_20261114_045012 --all-marks
rem   retrace replay-pdr reports\weartest_20261114_045012
rem   retrace pdr-calib
rem   retrace pdr-loop-test --label swing
rem   retrace pdr-loop-test --label carry
rem   retrace pdr-selftest
rem   retrace pdr-tune
rem   retrace pdr-tune --apply
rem   retrace pdr-report
rem   retrace pdr-import
rem   retrace pdr-import --listen-s 30
rem   retrace pocket
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo The Python environment .venv is missing from %cd%.
  exit /b 1
)
".venv\Scripts\python.exe" retrace_ring.py %*
