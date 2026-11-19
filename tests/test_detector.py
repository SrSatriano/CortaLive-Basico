"""Testes do detector de momentos (só áudio): sinais sintéticos (numpy) e um áudio curto gerado com ffmpeg."""
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from cortalive import config
from cortalive.detector import (
    ajustar_bordas,
    candidatos,
    energia_audio,
    grade_uniforme,
    normalizar_robusto,
)

PASSO = 0.5
DURACAO_TRECHO = 300.0
RUIDO_DB = 1.0
BASE_DB = -30.0
GRITO_DB = -5.0
SEMENTE = 1234


def _tempos(dur=DURACAO_TRECHO, passo=PASSO, t0=0.0):
    return t0 + np.arange(int(dur / passo)) * passo


def _audio_base(tempos, semente=SEMENTE):
    rng = np.random.default_rng(semente)
    return BASE_DB + rng.normal(0.0, RUIDO_DB, size=len(tempos))


def _rajada(serie, tempos, centro, valor, largura=3.0):
    serie = serie.copy()
    serie[np.abs(tempos - centro) <= largura / 2] = valor
    return serie


def _confere_limites(cands, tempos):
    lim_ini, lim_fim = tempos[0], tempos[-1] + PASSO
    for c in cands:
        assert set(c) >= {"ini", "fim", "pico", "score", "motivos"}
        assert lim_ini - 1e-6 <= c["ini"] <= c["pico"] <= c["fim"] <= lim_fim + 1e-6
        dur = c["fim"] - c["ini"]
        assert config.CORTE_MIN_SEG - 1e-6 <= dur <= config.CORTE_MAX_SEG + 1e-6
        assert isinstance(c["motivos"], list) and all(isinstance(m, str) for m in c["motivos"])


# ---------------------------------------------------------------- normalizar_robusto

def test_normalizar_robusto_destaca_pico_e_ignora_base():
    x = np.zeros(600) + 10.0 + np.random.default_rng(1).normal(0, 1, 600)
    x[300] = 30.0
    z = normalizar_robusto(x, janela_seg=120.0, passo=PASSO)
    assert z.shape == x.shape
    assert z[300] > 5
    assert np.median(np.abs(np.delete(z, 300))) < 1.5


def test_normalizar_robusto_nan_vazio_e_constante():
    assert normalizar_robusto(np.array([]), 60.0, PASSO).shape == (0,)
    x = np.full(100, 5.0)
    x[10] = np.nan
    x[20] = np.inf
    z = normalizar_robusto(x, 60.0, PASSO)
    assert np.all(np.isfinite(z))
    assert np.allclose(z, 0.0)


# ---------------------------------------------------------------- candidatos

def test_um_pico_obvio_de_audio():
    t = _tempos()
    audio = _rajada(_audio_base(t), t, 150.0, GRITO_DB)
    cands = candidatos(t, audio)
    assert len(cands) == 1
    c = cands[0]
    assert c["pico"] == pytest.approx(150.0, abs=2.0)
    assert c["ini"] == pytest.approx(c["pico"] - 0.6 * config.CORTE_ALVO_SEG, abs=0.01)
    assert c["fim"] == pytest.approx(c["pico"] + 0.4 * config.CORTE_ALVO_SEG, abs=0.01)
    assert any("grito/volume" in m and "σ" in m for m in c["motivos"])
    _confere_limites(cands, t)


def test_dois_picos_proximos_viram_um_so_nms():
    t = _tempos()
    audio = _rajada(_rajada(_audio_base(t), t, 150.0, GRITO_DB), t, 160.0, GRITO_DB + 2)
    cands = candidatos(t, audio)
    assert len(cands) == 1
    assert 145.0 <= cands[0]["pico"] <= 165.0


def test_dois_picos_distantes_ordenados_por_score():
    t = _tempos()
    audio = _rajada(_rajada(_audio_base(t), t, 60.0, GRITO_DB - 18), t, 220.0, GRITO_DB)
    cands = candidatos(t, audio)
    assert len(cands) == 2
    assert cands[0]["score"] >= cands[1]["score"]
    assert cands[0]["pico"] == pytest.approx(220.0, abs=2.0)
    assert cands[1]["pico"] == pytest.approx(60.0, abs=2.0)
    _confere_limites(cands, t)


def test_max_n_respeitado():
    t = _tempos(dur=900.0)
    audio = _audio_base(t)
    for centro in (100.0, 250.0, 400.0, 550.0, 700.0, 850.0):
        audio = _rajada(audio, t, centro, GRITO_DB)
    assert len(candidatos(t, audio, max_n=3)) == 3
    assert len(candidatos(t, audio, max_n=0)) == 0


def test_pico_na_borda_inicial_e_final():
    t = _tempos()
    inicio = candidatos(t, _rajada(_audio_base(t), t, 1.0, GRITO_DB))
    assert len(inicio) == 1
    assert inicio[0]["ini"] == pytest.approx(t[0])
    _confere_limites(inicio, t)
    fim = candidatos(t, _rajada(_audio_base(t), t, t[-1] - 1.0, GRITO_DB))
    assert len(fim) == 1
    assert fim[0]["fim"] == pytest.approx(t[-1] + PASSO)
    _confere_limites(fim, t)


def test_tempos_com_offset_de_live():
    t = _tempos(t0=3600.0)
    cands = candidatos(t, _rajada(_audio_base(t), t, 3750.0, GRITO_DB))
    assert cands[0]["pico"] == pytest.approx(3750.0, abs=2.0)
    _confere_limites(cands, t)


def test_trecho_mais_curto_que_corte_minimo_nao_gera_candidato():
    t = _tempos(dur=config.CORTE_MIN_SEG - 5)
    assert candidatos(t, _rajada(_audio_base(t), t, 5.0, GRITO_DB)) == []


def test_sinal_plano_nao_gera_candidato():
    t = _tempos()
    assert candidatos(t, np.full_like(t, BASE_DB)) == []
    assert candidatos(t, _audio_base(t)) == []


def test_entradas_vazias_nan_e_negativas():
    assert candidatos(np.array([]), np.array([])) == []
    t = _tempos()
    audio = _rajada(_audio_base(t), t, 150.0, GRITO_DB)
    audio[::7] = np.nan
    audio[5] = -np.inf
    cands = candidatos(t, audio)
    assert len(cands) >= 1
    assert all(np.isfinite(c["score"]) for c in cands)
    _confere_limites(cands, t)
    # áudio com tamanho diferente dos tempos não quebra
    assert candidatos(t, audio[:400])[0]["pico"] == pytest.approx(150.0, abs=2.0)


def test_buracos_nan_nao_viram_pico():
    t = _tempos()
    audio = _rajada(_audio_base(t), t, 150.0, GRITO_DB)
    audio[(t > 40) & (t < 80)] = np.nan          # bloco sem áudio
    cands = candidatos(t, audio)
    assert len(cands) == 1 and cands[0]["pico"] == pytest.approx(150.0, abs=2.0)


# ---------------------------------------------------------------- ajustar_bordas

def _palavras_continuas(ini, fim, dur_palavra=0.4, pausa=0.05):
    """Fala corrida (pausas curtíssimas) entre ini e fim, tempos absolutos."""
    out, t = [], ini
    while t + dur_palavra <= fim:
        out.append({"w": "bla", "s": round(t, 3), "e": round(t + dur_palavra, 3)})
        t += dur_palavra + pausa
    return out


def _dentro_de_palavra(t, palavras):
    return any(p["s"] < t < p["e"] for p in palavras)


def test_ajustar_bordas_move_para_pausa():
    cand = {"ini": 100.0, "fim": 145.0, "pico": 127.0, "score": 5.0, "motivos": ["x"]}
    # fala de 95 a 101.2, pausa de 1 s, fala de 102.2 a 144.1, pausa, fala de 145.3 a 150
    palavras = _palavras_continuas(95.0, 101.2) + _palavras_continuas(102.2, 144.1) + _palavras_continuas(145.3, 150)
    assert _dentro_de_palavra(100.0, palavras) or _dentro_de_palavra(145.0, palavras)
    rel = [{"w": p["w"], "s": p["s"] - 90.0, "e": p["e"] - 90.0} for p in palavras]
    novo = ajustar_bordas(cand, rel, inicio_trecho=90.0)
    assert not _dentro_de_palavra(novo["ini"], palavras)
    assert not _dentro_de_palavra(novo["fim"], palavras)
    assert 100.8 <= novo["ini"] <= 102.2     # dentro da pausa 100.8–102.2
    assert 144.0 <= novo["fim"] <= 145.4
    assert novo["pico"] == cand["pico"] and novo["motivos"] == cand["motivos"]
    assert cand["ini"] == 100.0   # não altera o original


def test_ajustar_bordas_padrao_relativo_ao_ini_do_candidato():
    cand = {"ini": 100.0, "fim": 140.0, "pico": 120.0, "score": 3.0, "motivos": []}
    palavras = [{"w": "a", "s": -0.5, "e": 0.3}, {"w": "b", "s": 1.5, "e": 2.0},
                {"w": "c", "s": 39.6, "e": 40.5}]
    novo = ajustar_bordas(cand, palavras)
    assert not _dentro_de_palavra(novo["ini"] - 100.0, palavras)
    assert not _dentro_de_palavra(novo["fim"] - 100.0, palavras)


def test_ajustar_bordas_sem_palavras_ou_lixo_nao_muda():
    cand = {"ini": 10.0, "fim": 50.0, "pico": 30.0, "score": 3.0, "motivos": []}
    assert ajustar_bordas(cand, []) == cand
    assert ajustar_bordas(cand, None) == cand
    lixo = [{"w": "a"}, {"w": "b", "s": "x", "e": 1}, {"w": "c", "s": 5, "e": 2}, None]
    assert ajustar_bordas(cand, lixo) == cand


def test_ajustar_bordas_respeita_duracao_minima_e_maxima():
    cand = {"ini": 0.0, "fim": float(config.CORTE_MIN_SEG), "pico": 10.0, "score": 3.0, "motivos": []}
    palavras = _palavras_continuas(-2.0, config.CORTE_MIN_SEG + 3.0)
    novo = ajustar_bordas(cand, palavras, inicio_trecho=0.0)
    assert config.CORTE_MIN_SEG - 1e-6 <= novo["fim"] - novo["ini"] <= config.CORTE_MAX_SEG + 1e-6
    assert novo["ini"] >= 0.0


# ---------------------------------------------------------------- energia_audio (ffmpeg real, áudio curto)

TEM_FFMPEG = Path(config.FFMPEG).exists() or shutil.which(config.FFMPEG) is not None
SILENCIO_SEG = 3
RAJADA_SEG = 2
TOTAL_SEG = 2 * SILENCIO_SEG + RAJADA_SEG


def _gerar_audio(destino: Path, com_rajada=True):
    amp = "0.9" if com_rajada else "0"
    expr = (f"aevalsrc=if(between(t\\,{SILENCIO_SEG}\\,{SILENCIO_SEG + RAJADA_SEG})\\,"
            f"{amp}*sin(2*PI*440*t)\\,0):s=16000:d={TOTAL_SEG}")
    subprocess.run([config.FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", expr, "-c:a", "pcm_s16le", str(destino)],
                   check=True)
    return destino


@pytest.mark.skipif(not TEM_FFMPEG, reason="ffmpeg não encontrado")
def test_energia_audio_arquivo_unico(tmp_path):
    wav = _gerar_audio(tmp_path / "a b.wav")
    tempos, db = energia_audio(wav, passo=PASSO)
    assert len(tempos) == len(db) == int(TOTAL_SEG / PASSO)
    assert tempos[0] == 0.0 and tempos[1] == pytest.approx(PASSO)
    alto = db[(tempos >= SILENCIO_SEG + 0.5) & (tempos < SILENCIO_SEG + RAJADA_SEG - 0.5)]
    baixo = db[tempos < SILENCIO_SEG - 0.5]
    assert alto.min() > -10.0
    assert baixo.max() < -60.0
    assert np.all(np.isfinite(db))


@pytest.mark.skipif(not TEM_FFMPEG, reason="ffmpeg não encontrado")
def test_energia_audio_segmentos_concatena_tempos_de_live(tmp_path):
    a = _gerar_audio(tmp_path / "seg_00000.wav")
    b = _gerar_audio(tmp_path / "seg_00001.wav")
    segs = [{"arquivo": b, "ini": 100.0 + TOTAL_SEG, "fim": 100.0 + 2 * TOTAL_SEG},
            {"arquivo": a, "ini": 100.0, "fim": 100.0 + TOTAL_SEG}]
    tempos, db = energia_audio(segs, passo=PASSO)
    n = int(TOTAL_SEG / PASSO)
    assert len(tempos) == 2 * n
    assert tempos[0] == pytest.approx(100.0)
    assert tempos[n] == pytest.approx(100.0 + TOTAL_SEG)
    assert np.all(np.diff(tempos) > 0)
    assert db[n + int((SILENCIO_SEG + 1) / PASSO)] > -10.0


@pytest.mark.skipif(not TEM_FFMPEG, reason="ffmpeg não encontrado")
def test_energia_audio_arquivo_ruim_nao_quebra(tmp_path):
    tempos, db = energia_audio(tmp_path / "nao_existe.ts")
    assert len(tempos) == 0 and len(db) == 0
    bom = _gerar_audio(tmp_path / "ok.wav")
    segs = [{"arquivo": tmp_path / "nao_existe.ts", "ini": 0.0, "fim": TOTAL_SEG},
            {"arquivo": bom, "ini": float(TOTAL_SEG), "fim": 2.0 * TOTAL_SEG}]
    tempos, db = energia_audio(segs)
    assert len(tempos) == int(TOTAL_SEG / PASSO)
    assert tempos[0] == pytest.approx(TOTAL_SEG)
    assert energia_audio([])[0].shape == (0,)


# ---------------------------------------------------------------- grade

def test_grade_uniforme_deixa_buraco_nos_vaos():
    # blocos [0,20] e [55,75] (emenda): áudio com buraco de 35 s
    t = np.concatenate([np.arange(0, 20, PASSO), np.arange(55, 75, PASSO)])
    db = np.concatenate([np.full(40, -30.0), np.full(40, -10.0)])
    grade, valores = grade_uniforme(t, db, PASSO)
    assert len(grade) == len(valores) == int(75 / PASSO)
    assert grade[0] == 0.0 and np.allclose(np.diff(grade), PASSO)
    assert np.isnan(valores[int(30 / PASSO)])
    assert valores[int(60 / PASSO)] == -10.0 and valores[int(10 / PASSO)] == -30.0
    vazia_t, vazia_v = grade_uniforme(np.array([]), np.array([]), PASSO)
    assert len(vazia_t) == len(vazia_v) == 0
