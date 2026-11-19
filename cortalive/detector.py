"""Detector de momentos: picos de energia do áudio (grito, risada, explosão) → candidatos a corte.

Vídeo local não tem chat ao vivo, então o sinal é só o áudio (o Pro também usa o ritmo do chat).
Só funções puras (numpy) mais a leitura de áudio via pipe do ffmpeg. Zero rede.
Todos os tempos são segundos do vídeo (0 = início).
"""
import subprocess
import threading
from collections import deque
from pathlib import Path

import numpy as np

from cortalive import config
from cortalive.util import log

ORIGEM_LOG = "detector"

# ---------- leitura de áudio ----------
TAXA_AMOSTRAGEM = 16000              # Hz, PCM mono
BYTES_POR_AMOSTRA = 2                # s16le
ESCALA_PCM = 32768.0                 # converte int16 → [-1, 1]
DB_PISO = -90.0                      # silêncio digital vira este valor (evita -inf)
JANELAS_POR_LEITURA = 240            # quantas janelas lidas do pipe por vez (memória baixa)
FRACAO_MIN_JANELA_FINAL = 0.5        # janela final incompleta só conta se tiver ao menos metade das amostras
LINHAS_STDERR_GUARDADAS = 20         # fim do stderr do ffmpeg guardado para o log de erro

# ---------- normalização ----------
FATOR_MAD_SIGMA = 1.4826             # MAD → desvio-padrão (normal)
ESCALA_MIN_PADRAO = 1e-6             # evita divisão por zero em sinal constante
AMOSTRAS_BASELINE_POR_JANELA = 20    # baseline em ~20 pontos por janela, interpolada (rápido em vídeo longo)
JANELA_BASELINE_SEG = 600.0          # baseline do vídeo: 10 min
Z_MAXIMO = 10.0                      # z saturado para nenhuma fonte dominar sozinha
ESCALA_MIN_AUDIO_DB = 1.0            # variação mínima considerada em dB

# ---------- score ----------
SUAVIZAR_AUDIO_SEG = 2.0
SCORE_MINIMO = 1.6                   # picos abaixo disso são ignorados (a IA filtra depois)
DISTANCIA_MIN_PICOS_SEG = float(config.CORTE_MAX_SEG)   # supressão de não-máximos
FRACAO_ANTES_PICO = 0.6              # 60% do corte antes do pico, 40% depois

# ---------- motivos ----------
LIMIAR_MOTIVO_Z = 2.0
VIZINHANCA_MOTIVO_SEG = 5.0

# ---------- ajuste de bordas ----------
PAUSA_MIN_SEG = 0.25                 # pausa de fala que serve de corte
MARGEM_FALA_SEG = 0.15               # folga entre a palavra e o corte
AJUSTE_MAX_SEG = 3.0                 # quanto a borda pode andar
PENALIDADE_PAUSA_CURTA = 1.0         # cortar numa pausa curtíssima (meio da frase)
PENALIDADE_CORTAR_PALAVRA = 10.0     # manter a borda no meio de uma palavra (último recurso)
OPCOES_POR_BORDA = 8


# =====================================================================================  áudio

def _janelas_db(amostras_int16, amostras_janela):
    """RMS em dB de janelas completas de `amostras_janela` amostras."""
    x = amostras_int16.astype(np.float64) / ESCALA_PCM
    n = len(x) // amostras_janela
    if n == 0:
        return np.empty(0)
    blocos = x[: n * amostras_janela].reshape(n, amostras_janela)
    rms = np.sqrt(np.mean(blocos * blocos, axis=1))
    return 20.0 * np.log10(np.maximum(rms, 10.0 ** (DB_PISO / 20.0)))


def _energia_arquivo(arquivo, passo):
    """(tempos relativos ao início do arquivo, db) lendo PCM do ffmpeg em pedaços."""
    amostras_janela = max(1, int(round(TAXA_AMOSTRAGEM * passo)))
    bytes_janela = amostras_janela * BYTES_POR_AMOSTRA
    cmd = [config.FFMPEG, "-nostdin", "-v", "error", "-i", str(arquivo), "-vn", "-ac", "1",
           "-ar", str(TAXA_AMOSTRAGEM), "-f", "s16le", "-acodec", "pcm_s16le", "pipe:1"]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as e:
        log(f"não consegui rodar o ffmpeg para {arquivo}: {e}", ORIGEM_LOG)
        return np.empty(0), np.empty(0)

    fim_stderr = deque(maxlen=LINHAS_STDERR_GUARDADAS)

    def _drenar():
        for linha in proc.stderr:
            fim_stderr.append(linha.decode("utf-8", "replace").rstrip())

    leitor = threading.Thread(target=_drenar, daemon=True)
    leitor.start()

    partes, sobra = [], b""
    while True:
        dados = proc.stdout.read(bytes_janela * JANELAS_POR_LEITURA)
        if not dados:
            break
        dados = sobra + dados
        util = len(dados) - len(dados) % bytes_janela
        if util:
            partes.append(_janelas_db(np.frombuffer(dados[:util], dtype="<i2"), amostras_janela))
        sobra = dados[util:]
    proc.wait()
    leitor.join()

    amostras_sobra = len(sobra) // BYTES_POR_AMOSTRA
    if amostras_sobra >= FRACAO_MIN_JANELA_FINAL * amostras_janela:
        resto = np.frombuffer(sobra[: amostras_sobra * BYTES_POR_AMOSTRA], dtype="<i2")
        partes.append(_janelas_db(resto, len(resto)))

    db = np.concatenate(partes) if partes else np.empty(0)
    if proc.returncode != 0:
        detalhe = " | ".join(fim_stderr)[-500:]
        if len(db) == 0:
            log(f"sem áudio em {Path(arquivo).name} (ffmpeg {proc.returncode}): {detalhe}", ORIGEM_LOG)
        else:
            log(f"áudio parcial em {Path(arquivo).name} (ffmpeg {proc.returncode}): {detalhe}", ORIGEM_LOG)
    return np.arange(len(db)) * passo, db


def energia_audio(arquivo_ou_segmentos, passo=0.5):
    """Volume (RMS em dB) por janela de `passo` s.

    Aceita um arquivo (tempos a partir de 0) ou lista de Segmentos (tempos de live concatenados, em ordem de `ini`).
    Arquivo ilegível/sem áudio é pulado (vira buraco nos tempos) e registrado no log.
    """
    if passo <= 0:
        raise ValueError("passo precisa ser positivo")
    if isinstance(arquivo_ou_segmentos, (str, Path)):
        fontes = [{"arquivo": arquivo_ou_segmentos, "ini": 0.0, "fim": None}]
    elif isinstance(arquivo_ou_segmentos, dict):
        fontes = [arquivo_ou_segmentos]
    else:
        fontes = sorted(arquivo_ou_segmentos or [], key=lambda s: float(s["ini"]))

    todos_t, todos_db = [], []
    for seg in fontes:
        t_rel, db = _energia_arquivo(seg["arquivo"], passo)
        ini = float(seg.get("ini") or 0.0)
        t = ini + t_rel
        fim = seg.get("fim")
        if fim is not None and float(fim) > ini:
            dentro = t < float(fim)          # arquivo um pouco mais longo que o bloco: não invade o próximo
            t, db = t[dentro], db[dentro]
        todos_t.append(t)
        todos_db.append(db)
    if not todos_t:
        return np.empty(0), np.empty(0)
    return np.concatenate(todos_t).astype(float), np.concatenate(todos_db).astype(float)


def grade_uniforme(tempos, valores, passo):
    """Põe uma série com buracos (vãos de emenda, bloco sem áudio) numa grade uniforme de `passo` s.

    Pontos sem dado viram NaN (não entram na baseline nem viram pico).
    Devolve (grade, valores_na_grade).
    """
    tempos = np.asarray(tempos, dtype=float).ravel()
    valores = np.asarray(valores, dtype=float).ravel()
    if len(tempos) == 0 or passo <= 0:
        return np.empty(0), np.empty(0)
    t0 = float(tempos[0])
    idx = np.round((tempos - t0) / passo).astype(int)
    n = int(idx.max()) + 1
    grade = t0 + np.arange(n) * passo
    saida = np.full(n, np.nan)
    validos = idx >= 0
    saida[idx[validos]] = valores[validos]
    return grade, saida


# =====================================================================================  normalização

def normalizar_robusto(x, janela_seg, passo, escala_min=ESCALA_MIN_PADRAO):
    """z-score robusto (mediana/MAD) numa janela móvel centrada de `janela_seg` (a baseline do vídeo).

    NaN/inf não entram na baseline e saem como z = 0.
    """
    x = np.asarray(x, dtype=float).ravel().copy()
    n = len(x)
    if n == 0:
        return np.empty(0)
    x[~np.isfinite(x)] = np.nan
    validos = np.isfinite(x)
    if not validos.any():
        return np.zeros(n)

    meia = max(1, int(round(janela_seg / max(passo, ESCALA_MIN_PADRAO) / 2)))
    salto = max(1, (2 * meia) // AMOSTRAS_BASELINE_POR_JANELA)
    centros = np.unique(np.append(np.arange(0, n, salto), n - 1))
    mediana_global = float(np.nanmedian(x))
    medianas, escalas = [], []
    for c in centros:
        trecho = x[max(0, c - meia): c + meia + 1]
        trecho = trecho[np.isfinite(trecho)]
        if len(trecho) == 0:
            med, mad = mediana_global, 0.0
        else:
            med = float(np.median(trecho))
            mad = float(np.median(np.abs(trecho - med)))
        medianas.append(med)
        escalas.append(max(FATOR_MAD_SIGMA * mad, escala_min))

    indices = np.arange(n)
    base = np.interp(indices, centros, medianas)
    escala = np.interp(indices, centros, escalas)
    z = (x - base) / escala
    z[~validos] = 0.0
    return z


def _suavizar(x, seg, passo):
    """Média móvel centrada (ímpar), corrigida nas bordas."""
    n = max(1, int(round(seg / passo)))
    if n % 2 == 0:
        n += 1
    if n == 1 or len(x) == 0:
        return x
    nucleo = np.ones(n)
    soma = np.convolve(x, nucleo, mode="same")
    conta = np.convolve(np.ones(len(x)), nucleo, mode="same")
    return soma / conta


def _alinhar(serie, n, nome):
    """Garante tamanho n (corta ou completa com NaN), avisando quando não bate."""
    if serie is None:
        return None
    serie = np.asarray(serie, dtype=float).ravel()
    if len(serie) == n:
        return serie.copy()
    if len(serie) > 0:
        log(f"{nome} com {len(serie)} pontos para {n} tempos; alinhando pelo índice", ORIGEM_LOG)
    saida = np.full(n, np.nan)
    m = min(n, len(serie))
    saida[:m] = serie[:m]
    return saida


def _z_positivo(x, passo, escala_min, suavizar_seg):
    z = normalizar_robusto(x, JANELA_BASELINE_SEG, passo, escala_min=escala_min)
    return _suavizar(np.clip(z, 0.0, Z_MAXIMO), suavizar_seg, passo)


# =====================================================================================  candidatos

def _janela_corte(pico, lim_ini, lim_fim):
    """ini/fim ao redor do pico com a duração-alvo, deslocada para caber no trecho."""
    dur = float(np.clip(config.CORTE_ALVO_SEG, config.CORTE_MIN_SEG, config.CORTE_MAX_SEG))
    ini = pico - FRACAO_ANTES_PICO * dur
    fim = pico + (1.0 - FRACAO_ANTES_PICO) * dur
    if ini < lim_ini:
        fim += lim_ini - ini
        ini = lim_ini
    if fim > lim_fim:
        ini -= fim - lim_fim
        fim = lim_fim
    ini = max(ini, lim_ini)
    return ini, fim


def _motivos(i, passo, z_audio):
    viz = max(1, int(round(VIZINHANCA_MOTIVO_SEG / passo)))
    za = float(np.max(z_audio[max(0, i - viz): i + viz + 1]))
    return [f"grito/volume {za:.1f}σ"] if za >= LIMIAR_MOTIVO_Z else []


def candidatos(tempos, audio_db, max_n=config.MAX_CANDIDATOS_POR_JANELA):
    """Momentos fortes do trecho, do maior score para o menor.

    score = z robusto positivo do volume (comparado com os 10 min em volta), suavizado. Picos com supressão de
    não-máximos (DISTANCIA_MIN_PICOS_SEG) e janela [pico - 60% do alvo, pico + 40% do alvo] presa ao trecho e a
    [CORTE_MIN_SEG, CORTE_MAX_SEG].
    """
    tempos = np.asarray(tempos, dtype=float).ravel()
    n = len(tempos)
    if n == 0 or max_n is None or max_n <= 0:
        return []
    if not np.all(np.isfinite(tempos)):
        log("tempos com NaN/inf; trecho ignorado", ORIGEM_LOG)
        return []
    passo = float(np.median(np.diff(tempos))) if n > 1 else 0.5
    if passo <= 0:
        log("tempos fora de ordem; trecho ignorado", ORIGEM_LOG)
        return []
    lim_ini, lim_fim = float(tempos[0]), float(tempos[-1] + passo)
    if lim_fim - lim_ini < config.CORTE_MIN_SEG:
        return []

    audio = _alinhar(audio_db, n, "áudio")
    score = _z_positivo(audio, passo, ESCALA_MIN_AUDIO_DB, SUAVIZAR_AUDIO_SEG)

    restante = score.copy()
    raio = int(np.ceil(DISTANCIA_MIN_PICOS_SEG / passo))
    saida = []
    while len(saida) < max_n:
        i = int(np.argmax(restante))
        valor = float(restante[i])
        if not np.isfinite(valor) or valor < SCORE_MINIMO:
            break
        restante[max(0, i - raio): i + raio + 1] = -np.inf
        pico = float(tempos[i])
        ini, fim = _janela_corte(pico, lim_ini, lim_fim)
        if fim - ini < config.CORTE_MIN_SEG - 1e-9:
            continue
        motivos = _motivos(i, passo, score) or [f"pico de áudio {valor:.1f}σ"]
        saida.append({"ini": round(ini, 3), "fim": round(fim, 3), "pico": round(pico, 3),
                      "score": round(valor, 3), "motivos": motivos})
    return saida


# =====================================================================================  bordas

def _palavras_validas(palavras, deslocamento):
    validas = []
    for p in palavras or []:
        try:
            s, e = float(p["s"]), float(p["e"])
        except (TypeError, KeyError, ValueError):
            continue
        if np.isfinite(s) and np.isfinite(e) and e >= s:
            validas.append((s + deslocamento, e + deslocamento))
    return sorted(validas)


def _opcoes_borda(t, palavras, eh_inicio, limite_ini):
    """Pontos de corte possíveis perto de t, com custo (distância + penalidade)."""
    m = MARGEM_FALA_SEG
    dentro_palavra = any(s < t < e for s, e in palavras)
    # vãos entre palavras: (fim da anterior, início da próxima); bordas abertas no começo e no fim
    vaos = [(-np.inf, palavras[0][0])]
    vaos += [(palavras[k][1], palavras[k + 1][0]) for k in range(len(palavras) - 1)]
    teto_final = max(t, palavras[-1][1] + m)
    vaos.append((palavras[-1][1], teto_final + m))

    opcoes = []
    em_pausa_boa = False
    for a, b in vaos:
        if b - a >= PAUSA_MIN_SEG:
            zona_ini, zona_fim = a + m, b - m
            if zona_ini > zona_fim:
                zona_ini = zona_fim = (a + b) / 2
            ponto = float(np.clip(t, zona_ini, zona_fim))
            penalidade = 0.0
            if a <= t <= b:
                em_pausa_boa = True
        elif b > a:
            ponto = (a + b) / 2
            penalidade = PENALIDADE_PAUSA_CURTA
        else:
            continue
        if not np.isfinite(ponto) or abs(ponto - t) > AJUSTE_MAX_SEG:
            continue
        if eh_inicio and ponto < limite_ini:
            continue
        opcoes.append((abs(ponto - t) + penalidade, ponto))

    if dentro_palavra:
        custo_original = PENALIDADE_CORTAR_PALAVRA
    elif em_pausa_boa:
        custo_original = 0.0
    else:
        custo_original = PENALIDADE_PAUSA_CURTA
    opcoes.append((custo_original, float(t)))
    opcoes.sort()
    return opcoes[:OPCOES_POR_BORDA]


def ajustar_bordas(cand, palavras, inicio_trecho=None):
    """Encaixa ini/fim do candidato em pausas de fala (sem cortar palavra no meio).

    `palavras` têm tempos relativos ao início do trecho transcrito; `inicio_trecho` é esse início em segundos de
    live (padrão: `cand["ini"]`, i.e. transcreveu-se exatamente o candidato). Cada borda anda no máximo
    AJUSTE_MAX_SEG, ini nunca vai antes do início transcrito, a duração fica em [CORTE_MIN_SEG, CORTE_MAX_SEG]
    e o pico continua dentro. Devolve um novo dict (o original não muda).
    """
    novo = dict(cand)
    ini, fim = float(cand["ini"]), float(cand["fim"])
    deslocamento = ini if inicio_trecho is None else float(inicio_trecho)
    lista = _palavras_validas(palavras, deslocamento)
    if not lista:
        return novo

    opcoes_ini = _opcoes_borda(ini, lista, True, deslocamento)
    opcoes_fim = _opcoes_borda(fim, lista, False, deslocamento)
    pico = cand.get("pico")
    melhor = None
    for custo_i, p_ini in opcoes_ini:
        for custo_f, p_fim in opcoes_fim:
            dur = p_fim - p_ini
            if not (config.CORTE_MIN_SEG - 1e-9 <= dur <= config.CORTE_MAX_SEG + 1e-9):
                continue
            if pico is not None and not (p_ini <= float(pico) <= p_fim):
                continue
            custo = custo_i + custo_f
            if melhor is None or custo < melhor[0]:
                melhor = (custo, p_ini, p_fim)
    if melhor is None:
        return novo
    novo["ini"], novo["fim"] = round(melhor[1], 3), round(melhor[2], 3)
    return novo
