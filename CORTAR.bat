@echo off
chcp 65001 >nul
setlocal EnableExtensions
title CortaLive Básico
cd /d "%~dp0"

echo.
echo  ==============================================
echo     CortaLive Básico - cortes verticais com IA
echo  ==============================================
echo.

rem ---------- 1) Python 3.11+ ----------
set "PY="
for %%C in ("py -3.13" "py -3.12" "py -3.11" "python" "py -3") do if not defined PY call :testar_python %%~C
if not defined PY goto :sem_python

rem ---------- 2) Ambiente do programa (só na 1a vez) ----------
if exist ".venv\Scripts\python.exe" goto :tem_venv
echo  Primeira vez: preparando o programa. Isso leva alguns minutos...
%PY% -m venv .venv
if errorlevel 1 goto :erro_instalar
:tem_venv
set "PYV=.venv\Scripts\python.exe"
if exist ".venv\instalado.ok" goto :tem_deps
echo  Instalando as dependências...
"%PYV%" -m pip install --upgrade pip >nul
"%PYV%" -m pip install -r requirements.txt
if errorlevel 1 goto :erro_instalar
echo ok> ".venv\instalado.ok"
echo  Pronto! Dependências instaladas.
echo.
:tem_deps

rem ---------- 3) ffmpeg ----------
"%PYV%" -c "import subprocess; from cortalive import config; subprocess.run([config.FFMPEG, '-version'], capture_output=True, check=True)" >nul 2>nul
if errorlevel 1 goto :sem_ffmpeg

rem ---------- 4) .env ----------
if exist ".env" goto :tem_env
copy /y ".env.exemplo" ".env" >nul
echo  Criei o arquivo .env com as configurações padrão.
echo  Para a IA escrever títulos e escolher os melhores momentos, abra o .env
echo  no Bloco de Notas e cole a sua chave - veja o README. Sem chave também funciona.
echo.
:tem_env

rem ---------- 5) Vídeo ----------
set "VIDEO=%~1"
if defined VIDEO goto :tem_video
echo  Dica: da próxima vez, arraste o vídeo e solte em cima do CORTAR.bat.
echo.
set /p "VIDEO=  Arraste o vídeo (ou a pasta com os .ts) para esta janela e aperte Enter: "
if not defined VIDEO goto :fim
set "VIDEO=%VIDEO:"=%"
:tem_video
if not exist "%VIDEO%" goto :nao_achei

echo.
set "CANAL="
set /p "CANAL=  Nome do criador do vídeo original, para os créditos (Enter = nome do arquivo): "
if defined CANAL set "CANAL=%CANAL:"=%"

echo.
echo  Cortando... a primeira vez baixa o modelo de transcrição, uns 500 MB.
echo.
if defined CANAL goto :cortar_com_canal
"%PYV%" -m cortalive cortar "%VIDEO%"
goto :depois
:cortar_com_canal
"%PYV%" -m cortalive cortar "%VIDEO%" --canal "%CANAL%"
:depois
if errorlevel 1 goto :erro_cortar
echo.
echo  Terminou! Os cortes estão na pasta "saida" ao lado do vídeo.
set "PASTA_SAIDA="
if exist "%VIDEO%\" set "PASTA_SAIDA=%VIDEO%\saida"
if not defined PASTA_SAIDA for %%I in ("%VIDEO%") do set "PASTA_SAIDA=%%~dpIsaida"
if exist "%PASTA_SAIDA%" start "" explorer "%PASTA_SAIDA%"
goto :fim

:erro_cortar
echo.
echo  Algo deu errado. Veja a mensagem acima. O log completo fica em dados\estado\cortalive.log
goto :fim

:nao_achei
echo.
echo  Não encontrei: "%VIDEO%"
goto :fim

:sem_python
echo  Não encontrei o Python 3.11 ou mais novo.
echo  1. Baixe em https://www.python.org/downloads/
echo  2. Na instalação, MARQUE a opção "Add python.exe to PATH".
echo  3. Depois rode este CORTAR.bat de novo.
goto :fim

:sem_ffmpeg
echo  O programa precisa do ffmpeg, uma ferramenta gratuita de vídeo, e ele não foi encontrado.
choice /c SN /m "  Instalar agora com o winget (Gyan.FFmpeg)"
if errorlevel 2 goto :ffmpeg_manual
winget install -e --id Gyan.FFmpeg
echo.
echo  Se a instalação terminou bem, rode o CORTAR.bat de novo.
goto :fim
:ffmpeg_manual
echo  Para instalar depois: abra o PowerShell e rode  winget install -e --id Gyan.FFmpeg
goto :fim

:erro_instalar
echo.
echo  Não consegui instalar as dependências. Confira a internet e tente de novo.
echo  Se continuar, apague a pasta .venv e rode o CORTAR.bat outra vez.
goto :fim

:fim
echo.
pause
endlocal
goto :eof

:testar_python
rem usa este Python se ele abrir de verdade e for 3.11 ou mais novo
%* -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if not errorlevel 1 set "PY=%*"
goto :eof
