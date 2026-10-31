@echo off
setlocal
pushd "%~dp0"
if errorlevel 1 exit /b 1
py -3.14 tools\build_windows.py %*
if errorlevel 1 goto failed
echo Executables and archive are in dist.
popd
exit /b 0
:failed
echo Build failed. Use 64-bit Python 3.14 and the .NET Framework 4 C# compiler.
popd
exit /b 1
