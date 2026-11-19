"""Acesso a um trecho [ini, fim] (segundos de live) espalhado em vários blocos .ts (ou num vídeo único).

Usa o concat demuxer do ffmpeg com uma lista de arquivos (aceita caminhos absolutos do Windows com -safe 0)
e busca precisa com -ss antes do -i (exata quando há reencode).
"""
import itertools
import threading
from pathlib import Path

from cortalive.config import FFMPEG, TEMP
from cortalive.util import rodar

_CONTADOR = itertools.count()
_LOCK = threading.Lock()

# Folga entre o fim de um bloco e o início do seguinte que ainda conta como "contínuo" (arredondamento do csv).
# Acima disso é um vão de emenda (reconexão do gravador): o concat demuxer junta os blocos SEM o buraco, então
# um trecho que atravessa o vão sairia mais curto e com os tempos deslocados.
TOLERANCIA_VAO_SEG = 0.5


def blocos_cobrindo(segmentos, ini, fim):
    """Blocos (em ordem) que têm alguma parte dentro de [ini, fim]."""
    if fim <= ini:
        return []
    ordenados = sorted(segmentos, key=lambda s: s["ini"])
    return [s for s in ordenados if s["fim"] > ini and s["ini"] < fim]


def trechos_continuos(segmentos, tolerancia=TOLERANCIA_VAO_SEG):
    """Intervalos [(ini, fim)] de tempo de live cobertos sem vão (blocos emendados até `tolerancia` s)."""
    trechos = []
    for s in sorted(segmentos or [], key=lambda s: float(s["ini"])):
        ini, fim = float(s["ini"]), float(s["fim"])
        if trechos and ini - trechos[-1][1] <= tolerancia:
            trechos[-1] = (trechos[-1][0], max(trechos[-1][1], fim))
        else:
            trechos.append((ini, fim))
    return trechos


def trecho_continuo_em(segmentos, t, tolerancia=TOLERANCIA_VAO_SEG):
    """Trecho contínuo (ini, fim) que contém o instante `t`; None se `t` cai num vão ou fora da gravação."""
    for ini, fim in trechos_continuos(segmentos, tolerancia):
        if ini <= float(t) <= fim:
            return ini, fim
    return None


def _nome_unico(prefixo, sufixo):
    with _LOCK:
        n = next(_CONTADOR)
    return TEMP / f"{prefixo}_{threading.get_ident()}_{n}{sufixo}"


def entrada_ffmpeg(segmentos, ini, fim):
    """Argumentos de entrada do ffmpeg para ler exatamente [ini, fim].

    Devolve (args, duracao, lista_tmp). Apague `lista_tmp` depois de rodar o ffmpeg.
    """
    blocos = blocos_cobrindo(segmentos, ini, fim)
    if not blocos:
        raise ValueError(f"nenhum bloco cobre o trecho {ini:.1f}–{fim:.1f}")
    for anterior, seguinte in zip(blocos, blocos[1:], strict=False):
        vao = float(seguinte["ini"]) - float(anterior["fim"])
        if vao > TOLERANCIA_VAO_SEG:
            raise ValueError(f"o trecho {ini:.1f}–{fim:.1f} atravessa um vão de {vao:.1f} s entre blocos "
                             f"(emenda em {float(anterior['fim']):.1f})")
    ini_real = max(ini, blocos[0]["ini"])
    deslocamento = ini_real - blocos[0]["ini"]
    # o concat junta os blocos sem buraco: a duração lida é a soma das partes cobertas (não fim - ini)
    duracao = sum(min(fim, float(b["fim"])) - max(ini, float(b["ini"])) for b in blocos)
    lista = _nome_unico("lista", ".txt")
    linhas = []
    for b in blocos:
        caminho = str(Path(b["arquivo"]).resolve()).replace("\\", "/").replace("'", "'\\''")
        linhas.append(f"file '{caminho}'")
    lista.write_text("\n".join(linhas) + "\n", encoding="utf-8")
    args = ["-ss", f"{deslocamento:.3f}", "-t", f"{duracao:.3f}", "-f", "concat", "-safe", "0", "-i", str(lista)]
    return args, duracao, lista


def extrair_trecho(segmentos, ini, fim, destino: Path, so_audio=False):
    """Grava [ini, fim] em `destino`. so_audio=True → WAV mono 16 kHz (para o Whisper)."""
    args, duracao, lista = entrada_ffmpeg(segmentos, ini, fim)
    try:
        if so_audio:
            cmd = [FFMPEG, "-y", "-v", "error", *args, "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
                   str(destino)]
        else:
            cmd = [FFMPEG, "-y", "-v", "error", *args, "-c", "copy", str(destino)]
        rodar(cmd)
    finally:
        lista.unlink(missing_ok=True)
    return destino, duracao
