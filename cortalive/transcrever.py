"""Transcrição local de trechos da live (faster-whisper na CPU, int8) com timestamp por palavra.

- Só o trecho [ini, fim] (segundos de live) é extraído para um WAV 16 kHz temporário (via `trechos.extrair_trecho`).
- Tempos das palavras saem RELATIVOS a `ini` (0 = início do trecho pedido).
- Resultado fica em cache JSON em `config.CACHE/transcricoes/<chave>.json`.
- O modelo é carregado uma única vez por processo (singleton com lock) e pode ser usado por várias threads.
"""
import itertools
import re
import threading
from pathlib import Path

from cortalive.config import CACHE, TEMP, WHISPER_IDIOMA, WHISPER_MODELO, WHISPER_THREADS
from cortalive.trechos import blocos_cobrindo, extrair_trecho
from cortalive.util import ler_json, log, salvar_json

PASTA_CACHE = CACHE / "transcricoes"
PASTA_TEMP = TEMP

# Parâmetros do Whisper (testados em pt-BR)
DISPOSITIVO = "cpu"
TIPO_COMPUTACAO = "int8"
TAMANHO_FEIXE = 3                     # beam_size
SILENCIO_MIN_VAD_MS = 300             # pausa mínima que o VAD considera silêncio
CASAS_TEMPO = 3                       # arredondamento dos tempos (ms)
TAMANHO_MAX_CHAVE = 150               # nome de arquivo de cache não pode crescer sem limite

_MODELO = None
_LOCK_MODELO = threading.Lock()
_LOCKS_CHAVE = {}
_LOCK_CHAVES = threading.Lock()
_CONTADOR_WAV = itertools.count()


# ---------- modelo (singleton) ----------
def _criar_modelo():
    """Carrega o WhisperModel (demora; baixa do Hugging Face na primeira vez)."""
    from faster_whisper import WhisperModel
    log(f"Carregando Whisper '{WHISPER_MODELO}' ({DISPOSITIVO}/{TIPO_COMPUTACAO}, {WHISPER_THREADS} threads)…",
        "transcrever")
    return WhisperModel(WHISPER_MODELO, device=DISPOSITIVO, compute_type=TIPO_COMPUTACAO,
                        cpu_threads=WHISPER_THREADS)


def obter_modelo():
    """Devolve o modelo do processo, criando na primeira chamada (seguro entre threads)."""
    global _MODELO
    if _MODELO is not None:
        return _MODELO
    with _LOCK_MODELO:
        if _MODELO is None:
            _MODELO = _criar_modelo()
    return _MODELO


# ---------- cache ----------
def caminho_cache(chave_cache: str) -> Path:
    """Arquivo de cache da chave, sempre dentro de PASTA_CACHE (sem separadores nem '..')."""
    nome = re.sub(r"[^\w.\-]+", "_", str(chave_cache).strip())
    nome = re.sub(r"\.{2,}", "_", nome).strip("._")[:TAMANHO_MAX_CHAVE]
    if not nome:
        raise ValueError(f"chave de cache inválida: {chave_cache!r}")
    return PASTA_CACHE / f"{nome}.json"


def _lock_da_chave(caminho: Path):
    """Um lock por arquivo de cache: duas threads pedindo o mesmo trecho não transcrevem em dobro."""
    with _LOCK_CHAVES:
        return _LOCKS_CHAVE.setdefault(str(caminho), threading.Lock())


def _cache_valido(dados):
    return isinstance(dados, dict) and isinstance(dados.get("palavras"), list) and isinstance(dados.get("texto"), str)


# ---------- conversão da saída do Whisper ----------
def mapear_palavras(segmentos_whisper, deslocamento: float):
    """Segmentos do faster-whisper → lista de Palavra, somando `deslocamento` (s) a cada tempo.

    Descarta palavras vazias, garante s >= 0 e e >= s.
    """
    palavras = []
    for seg in segmentos_whisper:
        for w in (getattr(seg, "words", None) or []):
            texto = (w.word or "").strip()
            if not texto:
                continue
            ini = max(0.0, float(w.start) + deslocamento)
            fim = max(ini, float(w.end) + deslocamento)
            palavras.append({"w": texto, "s": round(ini, CASAS_TEMPO), "e": round(fim, CASAS_TEMPO)})
    return palavras


def _whisper(caminho_wav: Path):
    modelo = obter_modelo()
    segmentos, _info = modelo.transcribe(
        str(caminho_wav), language=WHISPER_IDIOMA, word_timestamps=True, vad_filter=True,
        vad_parameters={"min_silence_duration_ms": SILENCIO_MIN_VAD_MS}, beam_size=TAMANHO_FEIXE,
        condition_on_previous_text=False,
    )
    return list(segmentos)   # o gerador só roda de fato aqui


# ---------- API pública ----------
def transcrever_trecho(segmentos, ini: float, fim: float, chave_cache: str) -> dict:
    """Transcreve [ini, fim] (segundos de live) → {"palavras": [Palavra], "texto": str}.

    Tempos das palavras relativos a `ini`. Usa/grava cache por `chave_cache`.
    Levanta ValueError se o trecho for inválido ou nenhum bloco o cobrir.
    """
    arq_cache = caminho_cache(chave_cache)
    with _lock_da_chave(arq_cache):
        if arq_cache.exists():
            dados = ler_json(arq_cache)
            if _cache_valido(dados):
                return {"palavras": dados["palavras"], "texto": dados["texto"]}
            log(f"cache de transcrição corrompido, refazendo: {arq_cache.name}", "transcrever")

        if fim <= ini:
            raise ValueError(f"trecho inválido: {ini:.1f}–{fim:.1f}")
        blocos = blocos_cobrindo(segmentos, ini, fim)
        if not blocos:
            raise ValueError(f"nenhum bloco cobre o trecho {ini:.1f}–{fim:.1f}")
        # O áudio extraído começa no máximo entre ini e o início do 1º bloco disponível.
        deslocamento = max(ini, float(blocos[0]["ini"])) - ini

        PASTA_TEMP.mkdir(parents=True, exist_ok=True)
        wav = PASTA_TEMP / f"transcrever_{threading.get_ident()}_{next(_CONTADOR_WAV)}.wav"
        try:
            extrair_trecho(segmentos, ini, fim, wav, so_audio=True)
            segs_whisper = _whisper(wav)
        finally:
            wav.unlink(missing_ok=True)

        palavras = mapear_palavras(segs_whisper, deslocamento)
        texto = " ".join(p["w"] for p in palavras)
        salvar_json(arq_cache, {"palavras": palavras, "texto": texto, "ini": ini, "fim": fim,
                                "modelo": WHISPER_MODELO})
        log(f"transcrito {ini:.1f}–{fim:.1f}: {len(palavras)} palavras", "transcrever")
        return {"palavras": palavras, "texto": texto}
