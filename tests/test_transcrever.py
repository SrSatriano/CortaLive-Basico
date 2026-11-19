"""Testes do módulo de transcrição (offline: o Whisper e o ffmpeg são simulados com monkeypatch)."""
import json
import os
import subprocess
import threading
from types import SimpleNamespace

import pytest

from cortalive import transcrever
from cortalive.config import FFMPEG

RODAR_LENTOS = os.environ.get("CORTES_TESTES_LENTOS") == "1"


# ---------- dublês ----------
def _palavra(w, s, e, p=0.9):
    return SimpleNamespace(word=w, start=s, end=e, probability=p)


def _seg_whisper(texto, palavras):
    return SimpleNamespace(text=texto, start=palavras[0].start if palavras else 0.0,
                           end=palavras[-1].end if palavras else 0.0, words=palavras)


class ModeloFalso:
    """Imita WhisperModel.transcribe: devolve (gerador de segmentos, info)."""

    def __init__(self, segmentos):
        self.segmentos = segmentos
        self.chamadas = []

    def transcribe(self, caminho, **kwargs):
        self.chamadas.append((caminho, kwargs))
        return iter(self.segmentos), SimpleNamespace(duration=30.0, language="pt")


@pytest.fixture
def ambiente(tmp_path, monkeypatch):
    """Cache em tmp, extração de áudio falsa (cria um wav vazio) e sem modelo carregado."""
    monkeypatch.setattr(transcrever, "PASTA_CACHE", tmp_path / "transcricoes")
    monkeypatch.setattr(transcrever, "PASTA_TEMP", tmp_path / "temp")
    monkeypatch.setattr(transcrever, "_MODELO", None)
    extracoes = []

    def extrair_falso(segmentos, ini, fim, destino, so_audio=False):
        assert so_audio is True
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_bytes(b"RIFF")
        extracoes.append((ini, fim, destino))
        return destino, fim - ini

    monkeypatch.setattr(transcrever, "extrair_trecho", extrair_falso)
    return SimpleNamespace(pasta=tmp_path, extracoes=extracoes)


SEGS_LIVE = [{"arquivo": "seg_00000.ts", "ini": 100.0, "fim": 200.0},
             {"arquivo": "seg_00001.ts", "ini": 200.0, "fim": 300.0}]


# ---------- cache ----------
def test_cache_existente_nao_chama_whisper(ambiente, monkeypatch):
    dados = {"palavras": [{"w": "oi", "s": 0.5, "e": 0.9}], "texto": "oi"}
    arq = ambiente.pasta / "transcricoes" / "live_abc_120.json"
    arq.parent.mkdir(parents=True)
    arq.write_text(json.dumps(dados), encoding="utf-8")

    def proibido(*a, **k):
        raise AssertionError("não deveria carregar o Whisper nem extrair áudio")

    monkeypatch.setattr(transcrever, "obter_modelo", proibido)
    monkeypatch.setattr(transcrever, "extrair_trecho", proibido)
    r = transcrever.transcrever_trecho(SEGS_LIVE, 120.0, 160.0, "live_abc_120")
    assert r == dados


def test_transcricao_nova_grava_cache_e_segunda_chamada_reaproveita(ambiente, monkeypatch):
    modelo = ModeloFalso([_seg_whisper(" Olha isso", [_palavra(" Olha", 1.0, 1.4), _palavra(" isso", 1.5, 1.9)])])
    monkeypatch.setattr(transcrever, "obter_modelo", lambda: modelo)
    r1 = transcrever.transcrever_trecho(SEGS_LIVE, 120.0, 160.0, "k1")
    assert (ambiente.pasta / "transcricoes" / "k1.json").exists()
    r2 = transcrever.transcrever_trecho(SEGS_LIVE, 120.0, 160.0, "k1")
    assert r1 == r2
    assert len(modelo.chamadas) == 1
    assert len(ambiente.extracoes) == 1


def test_parametros_do_whisper_e_wav_temporario_apagado(ambiente, monkeypatch):
    modelo = ModeloFalso([])
    monkeypatch.setattr(transcrever, "obter_modelo", lambda: modelo)
    transcrever.transcrever_trecho(SEGS_LIVE, 120.0, 160.0, "k2")
    caminho, kw = modelo.chamadas[0]
    assert kw["word_timestamps"] is True
    assert kw["vad_filter"] is True
    assert kw["language"] == transcrever.WHISPER_IDIOMA
    assert not os.path.exists(caminho)          # wav temporário removido
    assert ambiente.extracoes[0][:2] == (120.0, 160.0)


# ---------- mapeamento de tempos ----------
def test_tempos_relativos_a_ini_quando_trecho_esta_dentro_dos_blocos(ambiente, monkeypatch):
    modelo = ModeloFalso([
        _seg_whisper(" Meu Deus", [_palavra(" Meu", 2.0, 2.3), _palavra(" Deus!", 2.3, 2.8)]),
        _seg_whisper(" corre", [_palavra(" corre", 10.25, 10.75)]),
    ])
    monkeypatch.setattr(transcrever, "obter_modelo", lambda: modelo)
    r = transcrever.transcrever_trecho(SEGS_LIVE, 150.0, 190.0, "k3")
    assert r["palavras"] == [{"w": "Meu", "s": 2.0, "e": 2.3}, {"w": "Deus!", "s": 2.3, "e": 2.8},
                             {"w": "corre", "s": 10.25, "e": 10.75}]
    assert r["texto"] == "Meu Deus! corre"


def test_tempos_compensam_quando_ini_esta_antes_do_primeiro_bloco(ambiente, monkeypatch):
    # Trecho pedido começa em 90, mas o primeiro bloco só começa em 100: o wav começa em 100,
    # então uma palavra em 1.0 s do wav está em 11.0 s relativos a ini.
    modelo = ModeloFalso([_seg_whisper(" ei", [_palavra(" ei", 1.0, 1.5)])])
    monkeypatch.setattr(transcrever, "obter_modelo", lambda: modelo)
    r = transcrever.transcrever_trecho(SEGS_LIVE, 90.0, 130.0, "k4")
    assert r["palavras"] == [{"w": "ei", "s": 11.0, "e": 11.5}]


def test_mapear_palavras_descarta_vazias_e_corrige_tempos():
    segs = [_seg_whisper(" a", [_palavra("  ", 0.1, 0.2), _palavra(" b", 3.0, 2.5), _palavra(" c", -0.2, 0.4)]),
            _seg_whisper("", None)]
    palavras = transcrever.mapear_palavras(segs, deslocamento=0.0)
    assert palavras == [{"w": "b", "s": 3.0, "e": 3.0}, {"w": "c", "s": 0.0, "e": 0.4}]


def test_trecho_sem_fala_devolve_vazio(ambiente, monkeypatch):
    monkeypatch.setattr(transcrever, "obter_modelo", lambda: ModeloFalso([]))
    assert transcrever.transcrever_trecho(SEGS_LIVE, 120.0, 160.0, "k5") == {"palavras": [], "texto": ""}


def test_trecho_invalido_levanta_erro(ambiente):
    with pytest.raises(ValueError):
        transcrever.transcrever_trecho(SEGS_LIVE, 160.0, 120.0, "k6")
    with pytest.raises(ValueError):
        transcrever.transcrever_trecho(SEGS_LIVE, 500.0, 520.0, "k7")


# ---------- chave de cache ----------
def test_chave_de_cache_nao_escapa_da_pasta(ambiente):
    caminho = transcrever.caminho_cache("../../etc/passwd")
    assert caminho.parent == ambiente.pasta / "transcricoes"
    assert ".." not in caminho.name.replace(".json", "")
    with pytest.raises(ValueError):
        transcrever.caminho_cache("   ")


# ---------- singleton ----------
def test_modelo_carregado_uma_vez_com_varias_threads(monkeypatch):
    monkeypatch.setattr(transcrever, "_MODELO", None)
    criados = []
    barreira = threading.Barrier(8)

    def criar():
        criados.append(1)
        return object()

    monkeypatch.setattr(transcrever, "_criar_modelo", criar)
    resultados = []

    def tarefa():
        barreira.wait()
        resultados.append(transcrever.obter_modelo())

    threads = [threading.Thread(target=tarefa) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(criados) == 1
    assert len({id(r) for r in resultados}) == 1


# ---------- real (lento, precisa do modelo tiny no cache do HF) ----------
@pytest.mark.skipif(not RODAR_LENTOS, reason="lento: defina CORTES_TESTES_LENTOS=1 para rodar")
def test_real_com_modelo_tiny(tmp_path, monkeypatch):
    monkeypatch.setattr(transcrever, "WHISPER_MODELO", os.environ.get("CORTES_TESTE_MODELO", "tiny"))
    monkeypatch.setattr(transcrever, "_MODELO", None)
    monkeypatch.setattr(transcrever, "PASTA_CACHE", tmp_path / "transcricoes")
    bloco = tmp_path / "seg_00000.ts"
    subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=6",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=6", "-c:v", "libx264", "-c:a", "aac",
                    "-f", "mpegts", str(bloco)], check=True)
    r = transcrever.transcrever_trecho([{"arquivo": bloco, "ini": 0.0, "fim": 6.0}], 1.0, 5.0, "real_tiny")
    assert set(r) == {"palavras", "texto"}
    assert all(0.0 <= p["s"] <= p["e"] for p in r["palavras"])
