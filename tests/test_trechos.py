import subprocess

import pytest

from cortalive.config import FFMPEG
from cortalive.trechos import blocos_cobrindo, entrada_ffmpeg, extrair_trecho, trecho_continuo_em, trechos_continuos
from cortalive.util import probe


def _bloco(pasta, nome, dur, freq):
    arq = pasta / nome
    subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc2=size=320x180:rate=30:duration={dur}",
                    "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={dur}", "-c:v", "libx264", "-g", "30",
                    "-c:a", "aac", "-f", "mpegts", str(arq)], check=True)
    return arq


def test_blocos_cobrindo_seleciona_so_os_necessarios():
    segs = [{"arquivo": f"s{i}", "ini": i * 10.0, "fim": (i + 1) * 10.0} for i in range(5)]
    assert [s["arquivo"] for s in blocos_cobrindo(segs, 12, 27)] == ["s1", "s2"]
    assert blocos_cobrindo(segs, 30, 30) == []
    assert blocos_cobrindo(segs, 60, 70) == []
    assert [s["arquivo"] for s in blocos_cobrindo(segs, -5, 3)] == ["s0"]


def test_extrair_trecho_atravessa_emenda(tmp_path):
    a = _bloco(tmp_path, "seg 00000.ts", 6, 440)   # espaço no nome de propósito
    b = _bloco(tmp_path, "seg_00001.ts", 6, 880)
    segs = [{"arquivo": a, "ini": 0.0, "fim": 6.0}, {"arquivo": b, "ini": 6.0, "fim": 12.0}]
    wav, dur = extrair_trecho(segs, 4.0, 9.0, tmp_path / "t.wav", so_audio=True)
    assert dur == pytest.approx(5.0)
    assert wav.exists() and wav.stat().st_size > 16000 * 2 * 4
    mp4, _ = extrair_trecho(segs, 4.0, 9.0, tmp_path / "t.mp4")
    assert probe(mp4)["dur"] == pytest.approx(5.0, abs=1.2)   # -c copy corta em keyframe


def test_trecho_sem_bloco_levanta_erro():
    with pytest.raises(ValueError):
        extrair_trecho([{"arquivo": "x", "ini": 0, "fim": 5}], 10, 20, None)


# ---------------------------------------------------------------- vãos de emenda (reconexão do gravador)

def _segs_com_vao():
    """[0,20], [20,40] e, depois de uma reconexão, [55,75] (vão de 15 s em tempo de live)."""
    return [{"arquivo": "a", "ini": 0.0, "fim": 20.0}, {"arquivo": "b", "ini": 20.0, "fim": 40.0},
            {"arquivo": "c", "ini": 55.0, "fim": 75.0}]


def test_trechos_continuos_separa_nos_vaos():
    assert trechos_continuos(_segs_com_vao()) == [(0.0, 40.0), (55.0, 75.0)]
    assert trechos_continuos([]) == []
    quase = [{"arquivo": "a", "ini": 0.0, "fim": 10.0}, {"arquivo": "b", "ini": 10.02, "fim": 20.0}]
    assert trechos_continuos(quase) == [(0.0, 20.0)], "folga mínima do csv não é vão"


def test_trecho_continuo_de_um_instante():
    segs = _segs_com_vao()
    assert trecho_continuo_em(segs, 30.0) == (0.0, 40.0)
    assert trecho_continuo_em(segs, 60.0) == (55.0, 75.0)
    assert trecho_continuo_em(segs, 47.0) is None          # dentro do vão
    assert trecho_continuo_em(segs, 75.0) == (55.0, 75.0)  # borda final conta


def test_entrada_ffmpeg_recusa_trecho_que_atravessa_vao(isolado_temp):
    with pytest.raises(ValueError, match="vão"):
        entrada_ffmpeg(_segs_com_vao(), 30.0, 65.0)


def test_entrada_ffmpeg_duracao_soma_so_o_que_existe_com_folga_pequena(isolado_temp):
    segs = [{"arquivo": isolado_temp / "a.ts", "ini": 0.0, "fim": 10.0},
            {"arquivo": isolado_temp / "b.ts", "ini": 10.3, "fim": 20.0}]
    args, dur, lista = entrada_ffmpeg(segs, 5.0, 15.0)
    lista.unlink()
    assert dur == pytest.approx(9.7), "o concat emenda os blocos sem buraco: a duração é a soma das partes"


@pytest.fixture
def isolado_temp(tmp_path, monkeypatch):
    from cortalive import trechos
    monkeypatch.setattr(trechos, "TEMP", tmp_path)
    return tmp_path
