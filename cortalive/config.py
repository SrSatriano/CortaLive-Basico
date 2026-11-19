"""Configuração central do CortaLive Básico.

Tudo que é ajustável vem do arquivo `.env` (na pasta do programa ou na pasta de onde você roda o comando)
ou das variáveis de ambiente do Windows. Nenhum segredo fica aqui: as chaves de IA são suas e ficam no `.env`.
Veja o `.env.exemplo` para a lista completa, com explicação de cada item.
"""
import os
import shutil
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent          # pasta do programa
RECURSOS = RAIZ / "recursos"
FONTES = RECURSOS / "fontes"
MODELO_ROSTO = RECURSOS / "modelos" / "yunet.onnx"


def _carregar_env():
    """Lê o `.env` da pasta do programa e o da pasta atual. Variável já definida no Windows tem prioridade."""
    try:
        from dotenv import dotenv_values
    except ImportError:
        return
    vistos = set()
    for arq in (RAIZ / ".env", Path.cwd() / ".env"):
        try:
            if not arq.is_file() or arq.resolve() in vistos:
                continue
            vistos.add(arq.resolve())
        except OSError:
            continue
        for chave, valor in dotenv_values(arq).items():
            valor = (valor or "").strip()
            if valor and chave not in os.environ:       # comentários no fim da linha o dotenv já tira
                os.environ[chave] = valor


if not os.environ.get("CORTALIVE_SEM_ENV"):
    _carregar_env()


def _env_int(nome, padrao):
    try:
        return int(os.environ.get(nome, padrao))
    except ValueError:
        return padrao


def _env_float(nome, padrao):
    try:
        return float(os.environ.get(nome, padrao))
    except ValueError:
        return padrao


# ---------- pastas de trabalho (cache, progresso, temporários) ----------
DADOS = Path(os.environ.get("CORTALIVE_DADOS") or (RAIZ / "dados"))
CACHE = DADOS / "cache"                # transcrições e layout
ESTADO = DADOS / "estado"              # progresso de cada vídeo (para retomar) e log
TEMP = DADOS / "temp"
NOME_PASTA_SAIDA = "saida"             # padrão: <pasta do vídeo>/saida

for _p in (CACHE, ESTADO, TEMP):
    _p.mkdir(parents=True, exist_ok=True)


def _achar_ffmpeg(nome):
    """ffmpeg do PATH; se não houver, o instalado pelo winget (Gyan.FFmpeg)."""
    no_path = shutil.which(nome)
    if no_path:
        return no_path
    winget = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/WinGet/Packages"
    if winget.exists():
        for p in winget.glob(f"Gyan.FFmpeg*/**/bin/{nome}.exe"):
            return str(p)
    return nome


FFMPEG = os.environ.get("FFMPEG_PATH") or _achar_ffmpeg("ffmpeg")
FFPROBE = os.environ.get("FFPROBE_PATH") or _achar_ffmpeg("ffprobe")

# ---------- análise ----------
SEGMENTO_SEG = _env_int("SEGMENTO_SEG", 300)           # o vídeo é analisado em janelas de 5 min
# O TikTok só paga (Creator Rewards) vídeo com mais de 1 min; 72 s dá uma folga.
CORTE_MIN_SEG = _env_int("CORTE_MIN_SEG", 72)
CORTE_MAX_SEG = _env_int("CORTE_MAX_SEG", 150)
CORTE_ALVO_SEG = _env_int("CORTE_ALVO_SEG", 95)
MAX_CANDIDATOS_POR_JANELA = _env_int("MAX_CANDIDATOS_POR_JANELA", 4)
NOTA_MINIMA = _env_float("NOTA_MINIMA", 6.5)           # nota da IA (0–10) para virar corte
MAX_CORTES = _env_int("MAX_CORTES", 10)                # máximo de cortes por vídeo (0 = sem limite)
TEMA = os.environ.get("TEMA", "").strip()              # ex.: "Minecraft", "futebol" (vazio = genérico)

# ---------- transcrição ----------
WHISPER_MODELO = os.environ.get("WHISPER_MODELO", "small")
WHISPER_IDIOMA = os.environ.get("WHISPER_IDIOMA", "pt")
WHISPER_THREADS = _env_int("WHISPER_THREADS", min(8, os.cpu_count() or 4))

# ---------- processamento ----------
TRABALHADORES = _env_int("TRABALHADORES", 2)            # janelas analisadas ao mesmo tempo
FFMPEG_THREADS = _env_int("FFMPEG_THREADS", 0)          # 0 = o ffmpeg decide

# ---------- saída (vertical 1080x1920; o 4K é exclusivo do CortaLive Pro) ----------
OUT_W, OUT_H = 1080, 1920
OUT_FPS = _env_int("OUT_FPS", 30)
CRF = _env_int("CRF", 20)
X264_PRESET = os.environ.get("X264_PRESET", "veryfast")
LOUDNESS_LUFS = _env_float("LOUDNESS_LUFS", -14.0)
FONTE_LEGENDA = "Poppins Black"
FONTE_TITULO = "Anton"
MARCA = os.environ.get("MARCA", "")                     # marca d'água, ex.: @seucanal (vazio = desligada)
HASHTAGS_FIXAS = os.environ.get("HASHTAGS_FIXAS", "#cortes #shorts")
