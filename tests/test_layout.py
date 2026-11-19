"""Testes do módulo layout: funções puras com detecções sintéticas + vídeo sintético sem rosto."""
import subprocess

import pytest

from cortalive import config, layout
from cortalive.util import ler_json

W, H = 1920, 1080


def _rosto(x, y, lado=120):
    return [x, y, lado, lado]


# ---------- agrupar_rostos ----------

def test_agrupar_vazio_devolve_none():
    assert layout.agrupar_rostos([], W, H) is None
    assert layout.agrupar_rostos([[], [], []], W, H) is None


def test_agrupar_facecam_fixa_em_todas_as_amostras():
    deteccoes = [[_rosto(1650 + (i % 3) * 4, 820 - (i % 2) * 3)] for i in range(12)]
    rect = layout.agrupar_rostos(deteccoes, W, H)
    assert rect is not None
    x, y, w, h = rect
    assert 1640 <= x <= 1665 and 810 <= y <= 825
    assert 110 <= w <= 130 and 110 <= h <= 130


def test_agrupar_exige_metade_das_amostras():
    # rosto presente em 5 de 12 amostras (< 50%) -> não é facecam
    deteccoes = [[_rosto(100, 100)] if i < 5 else [] for i in range(12)]
    assert layout.agrupar_rostos(deteccoes, W, H) is None
    # 6 de 12 (= 50%) -> é facecam
    deteccoes = [[_rosto(100, 100)] if i < 6 else [] for i in range(12)]
    assert layout.agrupar_rostos(deteccoes, W, H) is not None


def test_agrupar_ignora_rosto_grande():
    # rosto com largura >= 22% da largura do vídeo (ex.: cutscene em close) não é facecam
    grande = int(W * 0.25)
    deteccoes = [[[800, 300, grande, grande]] for _ in range(12)]
    assert layout.agrupar_rostos(deteccoes, W, H) is None


def test_agrupar_ignora_rostos_que_andam_pela_tela():
    # NPCs do jogo aparecem em lugares diferentes a cada amostra
    deteccoes = [[_rosto(100 + i * 140, 100 + (i % 4) * 200)] for i in range(12)]
    assert layout.agrupar_rostos(deteccoes, W, H) is None


def test_agrupar_escolhe_a_regiao_fixa_entre_ruido():
    deteccoes = []
    for i in range(12):
        amostra = [_rosto(40, 40)]                      # facecam no canto superior esquerdo
        if i % 2 == 0:
            amostra.append(_rosto(300 + i * 100, 500))  # rosto de NPC passando
        deteccoes.append(amostra)
    x, y, _, _ = layout.agrupar_rostos(deteccoes, W, H)
    assert abs(x - 40) <= 2 and abs(y - 40) <= 2


def test_agrupar_entradas_invalidas_sao_ignoradas():
    deteccoes = [[[float("nan"), 10, 100, 100], [10, 10, -5, 100], _rosto(60, 60)] for _ in range(8)]
    rect = layout.agrupar_rostos(deteccoes, W, H)
    assert rect is not None and abs(rect[0] - 60) <= 2


def test_agrupar_frame_invalido():
    assert layout.agrupar_rostos([[_rosto(10, 10)]], 0, 0) is None


# ---------- expandir_cam ----------

def test_expandir_cam_proporcao_e_tamanho():
    x, y, w, h = layout.expandir_cam([900, 400, 100, 100], W, H)
    assert abs(w - 260) <= 2
    assert abs(w / h - 4 / 3) < 0.03
    # rosto continua dentro do retângulo
    assert x <= 900 and x + w >= 1000 and y <= 400 and y + h >= 500


def test_expandir_cam_preso_ao_frame_no_canto():
    for rosto in ([0, 0, 120, 120], [W - 120, H - 120, 120, 120], [W - 60, 0, 60, 60]):
        x, y, w, h = layout.expandir_cam(rosto, W, H)
        assert x >= 0 and y >= 0 and x + w <= W and y + h <= H
        assert w > 0 and h > 0


def test_expandir_cam_dimensoes_pares():
    x, y, w, h = layout.expandir_cam([501, 333, 97, 97], W, H)
    assert w % 2 == 0 and h % 2 == 0
    assert all(isinstance(v, int) for v in (x, y, w, h))


def test_expandir_cam_rosto_enorme_nao_passa_do_frame():
    x, y, w, h = layout.expandir_cam([0, 0, 1000, 1000], 1280, 720)
    assert w <= 1280 and h <= 720 and x >= 0 and y >= 0


# ---------- montar_layout ----------

def test_montar_layout_sem_rosto():
    assert layout.montar_layout(None, W, H) == {"tipo": "jogo", "jogo": [0, 0, W, H]}


def test_montar_layout_com_cam():
    lay = layout.montar_layout([1650, 820, 120, 120], W, H)
    assert lay["tipo"] == "cam_jogo"
    assert lay["jogo"] == [0, 0, W, H]
    x, y, w, h = lay["cam"]
    assert x + w <= W and y + h <= H


# ---------- detectar_layout (fluxo com detecção simulada) ----------

def _segmentos_falsos(pasta, fim):
    return [{"arquivo": pasta / "seg_00000.ts", "ini": 0.0, "fim": fim}]


def test_detectar_layout_com_facecam_simulada(tmp_path, monkeypatch):
    monkeypatch.setattr(layout, "_dimensoes", lambda segs: (W, H))
    monkeypatch.setattr(layout, "_rostos_nas_amostras",
                        lambda segs, tempos, w, h: [[_rosto(1650, 820)] for _ in tempos])
    lay = layout.detectar_layout(_segmentos_falsos(tmp_path, 600.0), amostras=12)
    assert lay["tipo"] == "cam_jogo"
    assert lay["jogo"] == [0, 0, W, H]


def test_tempos_amostra_espalhados():
    tempos = layout.tempos_amostra(100.0, 220.0, 12)
    assert len(tempos) == 12
    assert all(100.0 < t < 220.0 for t in tempos)
    assert tempos == sorted(tempos)
    assert layout.tempos_amostra(5.0, 5.0, 12) == []


def test_cache_por_live_e_revalidacao(tmp_path, monkeypatch):
    pasta_live = tmp_path / "20261118_120000"
    pasta_live.mkdir()
    chamadas = []

    def falsa(segs, tempos, w, h):
        chamadas.append(len(tempos))
        return [[_rosto(1650, 820)] for _ in tempos]

    monkeypatch.setattr(layout, "_dimensoes", lambda segs: (W, H))
    monkeypatch.setattr(layout, "_rostos_nas_amostras", falsa)

    lay1 = layout.detectar_layout(_segmentos_falsos(pasta_live, 600.0))
    arq = config.CACHE / "layout" / "20261118_120000.json"
    assert arq.exists()
    assert ler_json(arq)["layout"] == lay1
    assert len(chamadas) == 1

    # 10 min depois: usa o cache
    lay2 = layout.detectar_layout(_segmentos_falsos(pasta_live, 1200.0))
    assert lay2 == lay1 and len(chamadas) == 1

    # mais de 30 min depois da última análise: revalida
    layout.detectar_layout(_segmentos_falsos(pasta_live, 600.0 + layout.REVALIDAR_LAYOUT_SEG + 1))
    assert len(chamadas) == 2


def test_cache_corrompido_e_refeito(tmp_path, monkeypatch):
    pasta_live = tmp_path / "20261118_130000"
    pasta_live.mkdir()
    arq = config.CACHE / "layout" / "20261118_130000.json"
    arq.parent.mkdir(parents=True, exist_ok=True)
    arq.write_text("{lixo", encoding="utf-8")
    monkeypatch.setattr(layout, "_dimensoes", lambda segs: (W, H))
    monkeypatch.setattr(layout, "_rostos_nas_amostras", lambda segs, tempos, w, h: [[] for _ in tempos])
    lay = layout.detectar_layout(_segmentos_falsos(pasta_live, 600.0))
    assert lay == {"tipo": "jogo", "jogo": [0, 0, W, H]}
    assert ler_json(arq)["layout"] == lay


def test_falha_na_analise_cai_em_jogo(tmp_path, monkeypatch):
    def quebra(segs, tempos, w, h):
        raise RuntimeError("ffmpeg falhou")

    monkeypatch.setattr(layout, "_dimensoes", lambda segs: (W, H))
    monkeypatch.setattr(layout, "_rostos_nas_amostras", quebra)
    lay = layout.detectar_layout(_segmentos_falsos(tmp_path, 600.0))
    assert lay == {"tipo": "jogo", "jogo": [0, 0, W, H]}


# ---------- integração: vídeo sintético sem rosto ----------

@pytest.fixture
def video_sem_rosto(tmp_path):
    destino = tmp_path / "sintetico.mp4"
    cmd = [config.FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25:duration=6",
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(destino)]
    subprocess.run(cmd, check=True, capture_output=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return destino


def test_video_sintetico_sem_rosto_devolve_jogo(video_sem_rosto):
    lay = layout.detectar_layout(video_sem_rosto, amostras=4)
    assert lay == {"tipo": "jogo", "jogo": [0, 0, 1280, 720]}


def test_segmentos_sinteticos_sem_rosto_devolve_jogo(video_sem_rosto):
    segs = [{"arquivo": video_sem_rosto, "ini": 100.0, "fim": 106.0}]
    lay = layout.detectar_layout(segs, amostras=3)
    assert lay["tipo"] == "jogo"
    assert lay["jogo"] == [0, 0, 1280, 720]


# ---------------------------------------------------------------- ajustes com lives reais (2026-11-18)

def test_rosto_no_centro_nao_e_facecam():
    """Cena de 'papo' (cam ocupando a tela): rosto no meio do quadro vira layout só jogo, não cam duplicada."""
    from cortalive.layout import eh_overlay_de_cam, montar_layout
    rosto_centro = [900, 380, 200, 240]          # centro x = 1000 em 1920 → meio da tela
    assert not eh_overlay_de_cam(rosto_centro, 1920, 1080)
    assert montar_layout(rosto_centro, 1920, 1080)["tipo"] == "jogo"


def test_rosto_no_canto_continua_facecam():
    from cortalive.layout import eh_overlay_de_cam, montar_layout
    canto = [1650, 60, 150, 180]                 # canto superior direito
    assert eh_overlay_de_cam(canto, 1920, 1080)
    assert montar_layout(canto, 1920, 1080)["tipo"] == "cam_jogo"
    esquerda_baixo = [40, 800, 140, 170]
    assert eh_overlay_de_cam(esquerda_baixo, 1920, 1080)


def test_trecho_explicito_amostra_so_dentro_do_corte(monkeypatch):
    """Com ini/fim, as amostras ficam dentro do corte e o cache da live não é usado."""
    from cortalive import layout as L
    vistos = []

    def falso(segmentos, tempos, w, h):
        vistos.extend(tempos)
        return [[] for _ in tempos]

    monkeypatch.setattr(L, "_rostos_nas_amostras", falso)
    monkeypatch.setattr(L, "_dimensoes", lambda segs: (1920, 1080))
    segs = [{"arquivo": "x/20261118_072631/seg_00000.ts", "ini": 0.0, "fim": 300.0}]
    lay = L.detectar_layout(segs, ini=100.0, fim=140.0)
    assert lay["tipo"] == "jogo"
    assert vistos and all(100.0 <= t <= 140.0 for t in vistos)
