"""Testes do render vertical 1080x1920 (funções puras + integração com vídeo sintético)."""
import json
import re
import subprocess

import pytest

from cortalive import config
from cortalive import render as R
from cortalive.config import FFMPEG
from cortalive.util import probe

LIVE = {"plataforma": "twitch", "canal": "Criadôr Ç", "canal_id": "123", "url": "https://exemplo.com/x",
        "titulo": "LIVE DIA 1", "espectadores": 900, "id": "abc", "fixo": False}


def _corte(ini=1.0, fim=7.0):
    return {"ini": ini, "fim": fim, "pico": 4.0, "score": 3.2, "motivos": ["grito/volume 4.2σ"], "nota": 8.5,
            "titulo": "ELE ROUBOU O CARRO DA POLÍCIA", "texto_tela": "NÃO ACREDITO NISSO",
            "legenda_post": "Que isso!\nCréditos: Criadôr Ç (twitch)", "hashtags": ["#cortes", "#shorts"]}


# ---------- funções puras ----------
def test_nome_base_seguro_e_deterministico():
    n1 = R.nome_base(LIVE, _corte())
    assert n1 == R.nome_base(LIVE, _corte())
    assert re.fullmatch(r"[A-Za-z0-9_]+", n1)
    assert "Criador" in n1 and "ROUBOU" in n1
    assert n1 != R.nome_base(LIVE, _corte(ini=100.0, fim=140.0))
    assert len(n1) <= R.NOME_MAX_CHARS


def test_filtro_cam_jogo():
    lay = {"tipo": "cam_jogo", "cam": [1500, 700, 380, 300], "jogo": [0, 0, 1920, 1080]}
    f = R.montar_filtro(lay, 1920, 1080)
    assert "crop=380:300:1500:700" in f
    assert "vstack" in f and "pad=" in f
    assert f.rstrip().endswith("[v]")
    assert f"fps={config.OUT_FPS}" in f
    # todos os crops com números pares
    for w, h, _x, _y in re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", f):
        assert int(w) % 2 == 0 and int(h) % 2 == 0


def test_filtro_cam_jogo_fora_do_quadro_e_preso():
    lay = {"tipo": "cam_jogo", "cam": [1800, 1000, 500, 400], "jogo": [-50, -50, 3000, 3000]}
    f = R.montar_filtro(lay, 1920, 1080)
    for w, h, x, y in re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", f):
        assert int(x) + int(w) <= 1920 and int(y) + int(h) <= 1080
        assert int(w) >= 2 and int(h) >= 2


def test_filtro_jogo_fundo_desfocado():
    f = R.montar_filtro({"tipo": "jogo", "jogo": [0, 0, 1920, 1080]}, 1920, 1080)
    assert "boxblur" in f and "overlay" in f and "split" in f
    # frente: ~75% da largura do gameplay
    assert f"crop={int(1920 * R.ZOOM_FRENTE) // 2 * 2}:1080" in f
    assert f.rstrip().endswith("[v]")


@pytest.mark.parametrize("tipo", ["cam_jogo", "jogo"])
def test_filtro_zera_pts_antes_de_tudo(tipo):
    lay = {"tipo": tipo, "cam": [0, 0, 400, 300], "jogo": [0, 0, 1920, 1080]}
    assert R.montar_filtro(lay, 1920, 1080, ass="a.ass").startswith("[0:v]setpts=PTS-STARTPTS,")


def test_filtro_busca_lenta_corta_no_grafo():
    f = R.montar_filtro({"tipo": "jogo"}, 1920, 1080, trim=(3.5, 4.0))
    assert f.startswith("[0:v]setpts=PTS-STARTPTS,trim=start=3.500:duration=4.000,setpts=PTS-STARTPTS,")


def test_sem_busca_tira_ss_e_t():
    args = ["-ss", "3.500", "-t", "4.000", "-f", "concat", "-safe", "0", "-i", "lista.txt"]
    assert R._sem_busca(args) == (["-f", "concat", "-safe", "0", "-i", "lista.txt"], 3.5, 4.0)


def test_filtro_tipo_desconhecido_vira_jogo():
    assert R.montar_filtro({"tipo": "???"}, 1920, 1080) == R.montar_filtro({"tipo": "jogo"}, 1920, 1080)


def test_filtro_com_legendas_usa_caminho_relativo():
    f = R.montar_filtro({"tipo": "jogo"}, 1920, 1080, ass="corte.ass", fontsdir="fontes")
    assert "subtitles=f=corte.ass:fontsdir=fontes" in f
    assert ":\\" not in f and ":/" not in f.replace("f=corte", "")


def test_texto_post_tem_credito():
    txt = R.texto_post(LIVE, {**_corte(), "legenda_post": "Olha isso"})
    partes = txt.split("\n\n")
    assert partes[0] == "ELE ROUBOU O CARRO DA POLÍCIA"
    assert "Créditos: Criadôr Ç (twitch)" in txt
    assert "#cortes" in partes[-1]


def test_palavras_reposicionadas_no_tempo_do_corte():
    pal = [{"w": "a", "s": 0.5, "e": 1.0}, {"w": "b", "s": 12.0, "e": 12.4}]
    out = R.palavras_no_corte(pal, origem=90.0, ini=100.0, dur=10.0)
    assert out == [{"w": "b", "s": 2.0, "e": pytest.approx(2.4)}]


# ---------- integração ----------
def _bloco(pasta, nome, dur, freq, offset):
    arq = pasta / nome
    subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i",
                    f"testsrc2=size=1920x1080:rate=30:duration={dur}",
                    "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={dur}", "-c:v", "libx264", "-preset",
                    "ultrafast", "-g", "30", "-c:a", "aac", "-output_ts_offset", str(offset), "-f", "mpegts",
                    str(arq)], check=True)
    return arq


@pytest.fixture(scope="module")
def segmentos(tmp_path_factory):
    pasta = tmp_path_factory.mktemp("live")
    a = _bloco(pasta, "seg_00000.ts", 5, 440, 0)
    b = _bloco(pasta, "seg_00001.ts", 5, 880, 0)
    return [{"arquivo": a, "ini": 0.0, "fim": 5.0}, {"arquivo": b, "ini": 5.0, "fim": 10.0}]


@pytest.mark.parametrize("layout", [
    {"tipo": "cam_jogo", "cam": [1440, 700, 400, 300], "jogo": [0, 0, 1920, 1080]},
    {"tipo": "jogo", "jogo": [0, 0, 1920, 1080]},
], ids=["cam_jogo", "jogo"])
def test_renderizar_gera_corte_vertical(segmentos, layout, tmp_path, monkeypatch):
    def _sem_plano_b(*_a, **_k):
        raise AssertionError("a capa deveria sair do trecho, não do plano B")
    monkeypatch.setattr(R, "_capa_do_mp4", _sem_plano_b)
    pal =[{"w": w, "s": 0.3 + i * 0.4, "e": 0.6 + i * 0.4} for i, w in enumerate(
        "nossa ele pegou o carro da policia e saiu voando".split())]
    corte = _corte(1.0, 7.0)
    out = R.renderizar(segmentos, corte, layout, pal, LIVE, tmp_path / "saida" / "teste")
    for chave in ("mp4", "jpg", "post", "json"):
        assert out[chave].exists() and out[chave].stat().st_size > 0, chave
    info = probe(out["mp4"])
    assert (info["w"], info["h"]) == (config.OUT_W, config.OUT_H)
    assert info["dur"] == pytest.approx(6.0, abs=0.25)
    assert info["audio"]
    meta = json.loads(out["json"].read_text(encoding="utf-8"))
    assert meta["corte"]["titulo"] == corte["titulo"] and meta["layout"]["tipo"] == layout["tipo"]
    assert "Créditos" in out["post"].read_text(encoding="utf-8")
    capa = probe(out["jpg"])
    assert (capa["w"], capa["h"]) == (config.OUT_W, config.OUT_H)
    assert not list(config.TEMP.glob("render_*"))          # pasta temporária limpa


def test_renderizar_em_pasta_usa_nome_base(segmentos, tmp_path):
    out = R.renderizar(segmentos, _corte(2.0, 4.0), {"tipo": "jogo"}, [], LIVE, tmp_path)
    assert out["mp4"].parent == tmp_path
    assert out["mp4"].stem == R.nome_base(LIVE, _corte(2.0, 4.0))


def test_filtro_com_ramo_de_capa():
    f = R.montar_filtro({"tipo": "jogo"}, 1920, 1080, ass="corte.ass", fontsdir="fontes",
                        capa_ass="capa.ass", capa_t=3.25)
    assert "[vbase]split=2[vs][vc]" in f
    assert "select='gte(t,3.250)'" in f and "trim=end_frame=1" in f
    assert "subtitles=f=capa.ass:fontsdir=fontes" in f and f.endswith("[v]")
    assert "[capa]" in f and "yuvj420p" in f


def test_renderizar_keyframe_esparso_e_sem_audio(tmp_path, monkeypatch):
    """Bloco com 1 keyframe só e sem áudio: corte no meio do bloco ainda sai certo e ganha trilha silenciosa."""
    monkeypatch.setattr(R, "_capa_do_mp4", lambda *a, **k: (_ for _ in ()).throw(AssertionError("plano B")))
    arq = tmp_path / "seg_00000.ts"
    subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=8",
                    "-c:v", "libx264", "-preset", "ultrafast", "-g", "600", "-f", "mpegts", str(arq)], check=True)
    segs = [{"arquivo": arq, "ini": 0.0, "fim": 8.0}]
    corte = {**_corte(3.5, 7.5), "pico": 6.0}
    out = R.renderizar(segs, corte, {"tipo": "jogo", "jogo": [0, 0, 1280, 720]}, [], LIVE, tmp_path / "k")
    info = probe(out["mp4"])
    assert (info["w"], info["h"]) == (config.OUT_W, config.OUT_H)
    assert info["dur"] == pytest.approx(4.0, abs=0.25) and info["audio"]
    assert out["jpg"].stat().st_size > 0
