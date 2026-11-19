"""Testes do pipeline (cortador.py): janelas, dedup, progresso salvo, retomada, limite de cortes e CLI.

Quase tudo offline: os módulos pesados (detector, transcrever, curador, layout, render) são trocados por falsos.
No fim, um teste ponta a ponta de verdade com ffmpeg (sem IA e com transcrição falsa).
"""
import itertools
import subprocess
from pathlib import Path

import numpy as np
import pytest

import cortalive.cortador as cc
from cortalive import __main__ as cli
from cortalive import config
from cortalive.util import probe

SEG = 300.0


def blocos_fake(n, seg=SEG, pasta=Path("x")):
    return [{"arquivo": pasta / f"seg_{i:05d}.ts", "ini": i * seg, "fim": (i + 1) * seg} for i in range(n)]


def cand(pico, ini=None, fim=None, score=5.0):
    ini = pico - 27 if ini is None else ini
    fim = pico + 18 if fim is None else fim
    return {"ini": float(ini), "fim": float(fim), "pico": float(pico), "score": score, "motivos": ["grito/volume 4σ"]}


class Registro:
    """Guarda as chamadas feitas aos módulos falsos."""

    def __init__(self):
        self.candidatos = []
        self.notas = {}
        self.falhar_transcricao = set()
        self.falhar_render = False
        self.transcrever, self.curar, self.layout, self.render, self.energia = [], [], [], [], []


@pytest.fixture
def isolado(tmp_path, monkeypatch):
    for nome in ("ESTADO", "CACHE", "TEMP"):
        p = tmp_path / "dados" / nome.lower()
        p.mkdir(parents=True)
        monkeypatch.setattr(config, nome, p)
    return tmp_path


@pytest.fixture
def falsos(isolado, monkeypatch):
    """Troca o pipeline pesado por falsos. `reg.candidatos` define o que o detector devolve."""
    reg = Registro()

    def energia(segs, passo=0.5):
        reg.energia.append(segs)
        t1 = max(float(s["fim"]) for s in segs)
        tempos = np.arange(0, t1, passo)
        return tempos, np.full(len(tempos), -30.0)

    def candidatos(tempos, audio_db, max_n=4):
        t0, t1 = float(tempos[0]), float(tempos[-1])
        return [dict(c) for c in reg.candidatos if t0 <= c["pico"] <= t1][:max_n]

    def transcrever_trecho(segmentos, ini, fim, chave_cache):
        reg.transcrever.append((ini, fim, chave_cache))
        if round(ini) in reg.falhar_transcricao:
            raise RuntimeError("whisper explodiu")
        return {"palavras": [{"w": "nossa", "s": 1.0, "e": 1.4}], "texto": "nossa"}

    def curar(canal, titulo_live, candidatos_com_texto, plataforma=""):
        reg.curar.append({"cands": candidatos_com_texto, "canal": canal})
        return [{**{k: c[k] for k in ("ini", "fim", "pico", "score", "motivos")}, "id": i,
                 "nota": reg.notas.get(c["pico"], 8.0), "titulo": f"MOMENTO {int(c['pico'])}",
                 "texto_tela": "OLHA ISSO", "legenda_post": "Créditos: x", "hashtags": ["#cortes"], "origem": "ia"}
                for i, c in enumerate(candidatos_com_texto)]

    def detectar_layout(segs, amostras=12, chave_cache=None, ini=None, fim=None):
        reg.layout.append((ini, fim))
        return {"tipo": "jogo", "jogo": [0, 0, 1920, 1080]}

    def renderizar(segmentos, corte, layout, palavras, live, destino_base, origem_palavras=None):
        if reg.falhar_render:
            raise RuntimeError("ffmpeg falhou")
        reg.render.append({"corte": corte, "origem": origem_palavras, "destino": destino_base, "live": live,
                           "palavras": palavras})
        base = Path(destino_base) / f"corte_{int(corte['ini'])}"
        saidas = {"mp4": base.with_suffix(".mp4"), "jpg": base.with_suffix(".jpg"),
                  "post": base.with_name(base.name + "_POST.txt"), "json": base.with_suffix(".json")}
        for p in saidas.values():
            p.write_text("x", encoding="utf-8")
        return saidas

    monkeypatch.setattr(cc.detector, "energia_audio", energia)
    monkeypatch.setattr(cc.detector, "candidatos", candidatos)
    monkeypatch.setattr(cc.detector, "ajustar_bordas", lambda c, p, inicio_trecho=None: dict(c))
    monkeypatch.setattr(cc.transcrever, "transcrever_trecho", transcrever_trecho)
    monkeypatch.setattr(cc.curador, "curar", curar)
    monkeypatch.setattr(cc.layout, "detectar_layout", detectar_layout)
    monkeypatch.setattr(cc.render, "renderizar", renderizar)
    return reg


@pytest.fixture
def video_falso(isolado, monkeypatch):
    """Um 'vídeo' de 700 s (o probe é falso; o arquivo só precisa existir)."""
    video = isolado / "minha live.mp4"
    video.write_bytes(b"x")
    monkeypatch.setattr(cc.util, "probe", lambda p: {"w": 1920, "h": 1080, "fps": 60.0, "dur": 700.0, "audio": True})
    monkeypatch.setattr(config, "SEGMENTO_SEG", 300)
    return video


# ======================================================================================  janelas

def test_janelas_com_sobreposicao_e_zonas():
    js = cc.planejar_janelas(blocos_fake(3))
    assert [j["id"] for j in js] == ["seg_00000.ts", "seg_00001.ts", "seg_00002.ts", cc.ID_JANELA_FINAL]
    assert (js[0]["ini"], js[0]["fim"]) == (0, 300)
    assert (js[1]["ini"], js[1]["fim"]) == (0, 600)          # a janela do bloco N começa no bloco N-1
    assert js[1]["blocos"] == ["seg_00000.ts", "seg_00001.ts"]
    assert (js[0]["zona_ini"], js[0]["zona_fim"]) == (0, 150)
    assert (js[1]["zona_ini"], js[1]["zona_fim"]) == (150, 450)
    assert js[-1]["zona_ini"] == 750 and js[-1]["zona_fim"] >= 900


def test_zonas_contiguas_cobrem_o_video_inteiro():
    js = cc.planejar_janelas(blocos_fake(4))
    assert js[0]["zona_ini"] == 0
    for a, b in itertools.pairwise(js):
        assert a["zona_fim"] == pytest.approx(b["zona_ini"])


def test_cada_bloco_entra_em_duas_janelas():
    contagem = {}
    for j in cc.planejar_janelas(blocos_fake(4)):
        for b in j["blocos"]:
            contagem[b] = contagem.get(b, 0) + 1
    assert contagem == {f"seg_{i:05d}.ts": 2 for i in range(4)}


def test_janelas_sem_blocos_e_fora_de_ordem():
    assert cc.planejar_janelas([]) == []
    bs = blocos_fake(3)
    assert [j["id"] for j in cc.planejar_janelas([bs[2], bs[0], bs[1]])][:3] == \
        ["seg_00000.ts", "seg_00001.ts", "seg_00002.ts"]


def test_blocos_virtuais():
    bs = cc.blocos_virtuais(700.0, 300, Path("v.mp4"))
    assert [(b["ini"], b["fim"]) for b in bs] == [(0, 300), (300, 600), (600, 700)]
    assert len({b["nome"] for b in bs}) == 3
    assert all(b["arquivo"] == Path("v.mp4") for b in bs)
    assert cc.blocos_virtuais(0, 300, Path("v.mp4")) == []


def test_na_zona():
    j = {"zona_ini": 150.0, "zona_fim": 450.0}
    assert cc.na_zona(cand(150), j)
    assert cc.na_zona(cand(449.9), j)
    assert not cc.na_zona(cand(450), j)
    assert not cc.na_zona(cand(100), j)
    assert not cc.na_zona({"pico": float("nan")}, j)


# ======================================================================================  dedup e seleção

def test_ja_cortado():
    cortes = [{"ini": 100.0, "fim": 150.0}]
    assert cc.ja_cortado({"ini": 120, "fim": 170, "pico": 130}, cortes)        # pico dentro de corte existente
    assert cc.ja_cortado({"ini": 90, "fim": 140, "pico": 155}, cortes)         # 80% de sobreposição
    assert not cc.ja_cortado({"ini": 160, "fim": 200, "pico": 180}, cortes)
    assert not cc.ja_cortado({"ini": 140, "fim": 200, "pico": 185}, cortes)    # só 10 s em comum
    assert not cc.ja_cortado({"ini": 120, "fim": 170, "pico": 130}, [])
    assert not cc.ja_cortado({"ini": "lixo"}, cortes)


def test_selecionar_cortes_ordena_por_nota_limita_e_nao_repete():
    escolhidos = [{**cand(100), "nota": 7.0}, {**cand(400), "nota": 9.5}, {**cand(405), "nota": 8.0},
                  {**cand(650), "nota": 8.5}]
    sel = cc.selecionar_cortes(escolhidos, [], limite=2)
    assert [c["pico"] for c in sel] == [400, 650]
    assert [c["pico"] for c in cc.selecionar_cortes(escolhidos, [], limite=0)] == [400, 650, 100]
    feitos = [{"ini": 373.0, "fim": 418.0, "pico": 400.0}]
    assert [c["pico"] for c in cc.selecionar_cortes(escolhidos, feitos, limite=3)] == [650, 100]
    assert cc.selecionar_cortes(escolhidos, feitos, limite=1) == []


def test_prender_ao_trecho():
    segs = [{"ini": 0.0, "fim": 100.0}, {"ini": 100.0, "fim": 200.0}]
    assert cc.prender_ao_trecho(cand(150), segs) == cand(150)
    preso = cc.prender_ao_trecho(cand(10, ini=-17, fim=28), segs)
    assert preso["ini"] == 0 and preso["fim"] == pytest.approx(45)
    assert cc.prender_ao_trecho(cand(500), segs) is None


# ======================================================================================  utilidades

def test_ordenar_natural():
    nomes = [Path("bloco10.ts"), Path("bloco2.ts"), Path("bloco1.ts")]
    assert [p.name for p in cc.ordenar_natural(nomes)] == ["bloco1.ts", "bloco2.ts", "bloco10.ts"]


def test_chave_do_caminho_estavel_e_diferente_por_pasta(tmp_path):
    a, b = tmp_path / "a" / "live.mp4", tmp_path / "b" / "live.mp4"
    assert cc.chave_do_caminho(a) == cc.chave_do_caminho(a)
    assert cc.chave_do_caminho(a) != cc.chave_do_caminho(b)
    assert cc.chave_do_caminho(a).startswith("live_")


def test_pasta_saida_padrao(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    assert cc.pasta_saida_padrao(video) == tmp_path / "saida"
    assert cc.pasta_saida_padrao(tmp_path) == tmp_path / "saida"


# ======================================================================================  progresso salvo

def test_estado_persiste_e_retoma(tmp_path):
    arq = tmp_path / "p.json"
    e = cc.Estado(arq)
    e.marcar_janela("v00000", [{**cand(100), "nota": 8.0}])
    e.registrar_corte({"ini": 73.0, "fim": 118.0, "mp4": "x.mp4"})
    e2 = cc.Estado(arq)
    assert e2.janela_feita("v00000") and not e2.janela_feita("v00001")
    assert len(e2.escolhidos()) == 1 and len(e2.cortes()) == 1
    e2.esquecer()
    assert not cc.Estado(arq).janela_feita("v00000")


def test_estado_corrompido_comeca_vazio(tmp_path):
    arq = tmp_path / "p.json"
    arq.write_text("{lixo", encoding="utf-8")
    e = cc.Estado(arq)
    assert e.escolhidos() == [] and e.cortes() == []
    e.marcar_janela("x")
    assert cc.Estado(arq).janela_feita("x")


def test_estado_falhando_ao_salvar_nao_propaga(tmp_path, monkeypatch):
    def colide(*_a, **_k):
        raise PermissionError(13, "Acesso negado")

    monkeypatch.setattr(cc, "salvar_json", colide)
    e = cc.Estado(tmp_path / "p.json")
    e.marcar_janela("x")                        # não levanta
    assert e.janela_feita("x")


# ======================================================================================  uma janela

def _fonte(video):
    return cc.FonteVideo(video, canal="Fulano", plataforma="twitch")


def test_pipeline_de_uma_janela(falsos, video_falso):
    falsos.candidatos = [cand(100), cand(200)]
    fonte = _fonte(video_falso)
    janela = cc.planejar_janelas(fonte.blocos())[0]           # zona [0, 150)
    escolhidos = cc.processar_janela(fonte, janela)
    assert [c["pico"] for c in escolhidos] == [100]           # 200 fica para a janela seguinte
    assert escolhidos[0]["palavras"] and escolhidos[0]["trans_ini"] == pytest.approx(100 - 27 - 10)
    ini, fim, chave = falsos.transcrever[0]
    assert (ini, fim) == (pytest.approx(63.0), pytest.approx(128.0)) and chave.startswith(fonte.chave)
    assert falsos.curar[0]["canal"]["canal"] == "Fulano"


def test_falha_na_transcricao_pula_so_aquele_candidato(falsos, video_falso):
    falsos.candidatos = [cand(330), cand(420)]
    falsos.falhar_transcricao = {330 - 27 - 10}
    fonte = _fonte(video_falso)
    janela = cc.planejar_janelas(fonte.blocos())[1]           # zona [150, 450)
    assert [c["pico"] for c in cc.processar_janela(fonte, janela)] == [420]


def test_todas_as_transcricoes_falhando_e_erro_temporario(falsos, video_falso):
    falsos.candidatos = [cand(100)]
    falsos.falhar_transcricao = {100 - 27 - 10}
    fonte = _fonte(video_falso)
    with pytest.raises(cc.ErroTemporario):
        cc.processar_janela(fonte, cc.planejar_janelas(fonte.blocos())[0])


def test_erro_temporario_tenta_de_novo_e_depois_desiste(falsos, video_falso, tmp_path, monkeypatch):
    chamadas = []

    def sempre_temporario(fonte, janela):
        chamadas.append(janela["id"])
        raise cc.ErroTemporario("modelo baixando")

    monkeypatch.setattr(cc, "processar_janela", sempre_temporario)
    e = cc.Estado(tmp_path / "p.json")
    janela = cc.planejar_janelas(_fonte(video_falso).blocos())[0]
    assert cc._processar_com_tentativas(_fonte(video_falso), janela, e, espera_base=0) == []
    assert len(chamadas) == cc.JANELA_MAX_TENTATIVAS_TEMPORARIO
    assert e.janela_feita(janela["id"])


def test_sem_som_nao_quebra(falsos, video_falso, monkeypatch):
    monkeypatch.setattr(cc.detector, "energia_audio", lambda segs, passo=0.5: (np.array([]), np.array([])))
    monkeypatch.setattr(cc.util, "probe", lambda p: {"w": 1920, "h": 1080, "fps": 30.0, "dur": 700.0, "audio": False})
    fonte = _fonte(video_falso)
    assert cc.processar_janela(fonte, cc.planejar_janelas(fonte.blocos())[0]) == []


# ======================================================================================  cortar (vídeo inteiro)

def test_cortar_video_processa_tudo_retoma_e_refaz(falsos, video_falso):
    falsos.candidatos = [cand(100), cand(400), cand(650)]
    cortes = cc.cortar(video_falso, canal="Fulano", trabalhadores=2)
    assert sorted(c["pico"] for c in cortes) == [100, 400, 650]
    assert len(falsos.energia) == 1                                      # áudio lido uma vez só
    saida = video_falso.parent / "saida"
    assert all(Path(c["mp4"]).parent == saida and Path(c["mp4"]).exists() for c in cortes)
    assert falsos.render[0]["live"]["canal"] == "Fulano"
    assert cc.cortar(video_falso, canal="Fulano") == []                  # retomado: nada a refazer
    assert len(cc.cortar(video_falso, canal="Fulano", refazer=True)) == 3


def test_cortar_respeita_max_cortes_e_escolhe_as_maiores_notas(falsos, video_falso):
    falsos.candidatos = [cand(100), cand(400), cand(650)]
    falsos.notas = {100: 7.0, 400: 9.0, 650: 8.0}
    cortes = cc.cortar(video_falso, max_cortes=2)
    assert [c["pico"] for c in cortes] == [400, 650]
    # rodar de novo com limite maior faz só o que faltou, sem reanalisar
    transcricoes = len(falsos.transcrever)
    assert [c["pico"] for c in cc.cortar(video_falso, max_cortes=5)] == [100]
    assert len(falsos.transcrever) == transcricoes


def test_cortar_pasta_de_saida_escolhida(falsos, video_falso, tmp_path):
    falsos.candidatos = [cand(100)]
    destino = tmp_path / "meus cortes"
    cortes = cc.cortar(video_falso, saida=destino)
    assert Path(cortes[0]["mp4"]).parent == destino


def test_cortar_credito_padrao_e_o_nome_do_arquivo(falsos, video_falso):
    falsos.candidatos = [cand(100)]
    cc.cortar(video_falso)
    assert falsos.render[0]["live"]["canal"] == "minha live"


def test_falha_no_render_nao_registra(falsos, video_falso):
    falsos.candidatos = [cand(100)]
    falsos.falhar_render = True
    assert cc.cortar(video_falso) == []
    falsos.falhar_render = False
    assert len(cc.cortar(video_falso)) == 1                              # tenta de novo na próxima vez


def test_cortar_pasta_de_blocos_ts(falsos, isolado, monkeypatch):
    pasta = isolado / "gravacao"
    pasta.mkdir()
    for nome in ("parte10.ts", "parte2.ts", "parte1.ts", "leia.txt"):
        (pasta / nome).write_bytes(b"x")
    monkeypatch.setattr(cc.util, "probe", lambda p: {"w": 1280, "h": 720, "fps": 30.0, "dur": 300.0, "audio": True})
    fonte = cc.FontePasta(pasta)
    assert [s["nome"] for s in fonte.segmentos()] == ["parte1.ts", "parte2.ts", "parte10.ts"]
    assert [(s["ini"], s["fim"]) for s in fonte.segmentos()] == [(0, 300), (300, 600), (600, 900)]
    falsos.candidatos = [cand(100), cand(500)]
    cortes = cc.cortar(pasta, trabalhadores=1)
    assert sorted(c["pico"] for c in cortes) == [100, 500]
    assert Path(cortes[0]["mp4"]).parent == pasta / "saida"


def test_pasta_sem_ts_e_caminho_inexistente(isolado):
    vazia = isolado / "vazia"
    vazia.mkdir()
    with pytest.raises(FileNotFoundError):
        cc.cortar(vazia)
    with pytest.raises(FileNotFoundError):
        cc.cortar(isolado / "nao_existe.mp4")


# ======================================================================================  linha de comando

def test_cli_cortar_e_erro_amigavel(falsos, video_falso, capsys):
    falsos.candidatos = [cand(100)]
    assert cli.main(["cortar", str(video_falso), "--max-cortes", "1", "--canal", "Fulano"]) == 0
    assert "1 corte(s) novo(s)" in capsys.readouterr().out
    assert cli.main(["cortar", str(video_falso.parent / "nao_existe.mp4")]) == 1
    assert "Erro:" in capsys.readouterr().err


def test_cli_sem_comando_mostra_ajuda(capsys):
    assert cli.main([]) == 2
    assert "cortar" in capsys.readouterr().out


# ======================================================================================  ponta a ponta (ffmpeg real)

def _video_com_grito(caminho, dur=140, grito_em=80):
    """Vídeo 320x180 com som baixo e um 'grito' de 3 s alto em `grito_em`."""
    volume = f"volume='if(between(t,{grito_em},{grito_em + 3}),1.0,0.03)':eval=frame"
    subprocess.run([config.FFMPEG, "-y", "-v", "error",
                    "-f", "lavfi", "-i", f"testsrc2=size=320x180:rate=30:duration={dur}",
                    "-f", "lavfi", "-i", f"sine=frequency=440:duration={dur}",
                    "-af", volume, "-c:v", "libx264", "-preset", "ultrafast", "-g", "60",
                    "-c:a", "aac", "-shortest", str(caminho)], check=True)
    return caminho


def test_ponta_a_ponta_sem_ia(isolado, monkeypatch):
    """Detector, layout e render de verdade; transcrição falsa (sem baixar o Whisper) e sem chave de IA."""
    for nome in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "VERTEX_CRED"):
        monkeypatch.delenv(nome, raising=False)
    monkeypatch.setattr(config, "SEGMENTO_SEG", 300)
    monkeypatch.setattr(config, "NOTA_MINIMA", 1.0)
    palavras = "que explosao absurda mano".split()
    falas = [{"w": w, "s": 2.0 + i * 0.5, "e": 2.4 + i * 0.5} for i, w in enumerate(palavras)]
    monkeypatch.setattr(cc.transcrever, "transcrever_trecho",
                        lambda segs, ini, fim, chave: {"palavras": falas, "texto": " ".join(p["w"] for p in falas)})
    video = _video_com_grito(isolado / "teste.mp4")

    cortes = cc.cortar(video, canal="Criador Teste", plataforma="YouTube", max_cortes=1)

    assert len(cortes) == 1
    c = cortes[0]
    assert c["ini"] <= 80 <= c["fim"] and c["origem"] == "fallback"
    info = probe(c["mp4"])
    assert (info["w"], info["h"]) == (1080, 1920) and info["audio"]
    assert config.CORTE_MIN_SEG - 1 <= info["dur"] <= config.CORTE_MAX_SEG + 1
    post = Path(c["post"]).read_text(encoding="utf-8")
    assert "Créditos: Criador Teste (YouTube)" in post and "#cortes" in post
    assert Path(c["jpg"]).stat().st_size > 0


def test_saida_relativa_vira_absoluta(falsos, video_falso, monkeypatch):
    falsos.candidatos = [cand(100)]
    monkeypatch.chdir(video_falso.parent)
    cortes = cc.cortar(video_falso.name, saida="relativa")
    assert Path(cortes[0]["mp4"]).is_absolute()
    assert falsos.render[0]["destino"] == (video_falso.parent / "relativa").resolve()


def test_blocos_virtuais_sobra_de_segundos_entra_no_ultimo_bloco():
    """Vídeo de 300,03 s não pode virar um bloco de 0,03 s (janela sem áudio, tentativas perdidas)."""
    bs = cc.blocos_virtuais(300.03, 300, Path("v.ts"))
    assert len(bs) == 1 and bs[0]["fim"] == 300.03
    bs = cc.blocos_virtuais(604.0, 300, Path("v.ts"))
    assert [round(b["fim"], 2) for b in bs] == [300.0, 604.0]
