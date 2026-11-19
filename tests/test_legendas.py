"""Testes do gerador de legendas .ass (1080x1920)."""
import re

import pytest

from cortalive import config
from cortalive import legendas as L


def _pal(txt, s, e):
    return {"w": txt, "s": s, "e": e}


def _dialogos(ass):
    return [l for l in ass.splitlines() if l.startswith("Dialogue:")]


def _tempos(linha):
    campos = linha.split(",", 9)
    return L.ts_para_seg(campos[1]), L.ts_para_seg(campos[2]), campos[3]


# ---------- escape ----------
def test_escapar_remove_tags_barras_e_quebras():
    sujo = "{\\an8}OLHA\\NISSO\n agora\r\tJÁ {\\b1}"
    limpo = L.escapar(sujo)
    assert "{" not in limpo and "}" not in limpo and "\\" not in limpo
    assert "\n" not in limpo and "\r" not in limpo and "\t" not in limpo
    assert "  " not in limpo
    assert "OLHA" in limpo and "JÁ" in limpo


def test_escapar_vazio_e_none():
    assert L.escapar("") == ""
    assert L.escapar(None) == ""


def test_montar_ass_nao_deixa_injecao_passar():
    ass = L.montar_ass([_pal("oi{\\an8}", 0.0, 0.5)], "TÍTULO {\\pos(0,0)}\\NFALSO", "Canal\n{\\fs300}",
                       10.0, "jogo")
    for linha in _dialogos(ass):
        texto = linha.split(",", 9)[9]
        # tudo que está entre chaves são tags nossas; nenhuma tag injetada pode sobreviver
        assert "\\an8" not in texto and "\\fs300" not in texto and "\\pos(0,0)" not in texto
        assert texto.count("{") == texto.count("}")
    assert "\\NFALSO" not in ass


# ---------- tempos ----------
def test_ts_formato_ass():
    assert L.ts(0) == "0:00:00.00"
    assert L.ts(3661.5) == "1:01:01.50"
    assert L.ts(-3) == "0:00:00.00"
    assert L.ts_para_seg("1:01:01.50") == pytest.approx(3661.5)


def test_eventos_ficam_dentro_da_duracao():
    pal = [_pal("um", 0.2, 0.5), _pal("dois", 0.6, 0.9), _pal("tres", 9.5, 11.0), _pal("fora", 12.0, 12.5)]
    ass = L.montar_ass(pal, "TITULO", "canal", 10.0, "jogo")
    for linha in _dialogos(ass):
        a, b, _ = _tempos(linha)
        assert 0.0 <= a < b <= 10.0 + 1e-6
    assert "FORA" not in ass


def test_titulo_so_nos_primeiros_segundos():
    ass = L.montar_ass([], "GOLPE ÉPICO", "canal", 30.0, "jogo")
    tit = [l for l in _dialogos(ass) if _tempos(l)[2] == "Titulo"]
    assert len(tit) == 1
    a, b, _ = _tempos(tit[0])
    assert a == 0.0 and b == pytest.approx(L.TITULO_DUR_SEG, abs=0.01)
    assert "\\fad(" in tit[0] and "\\t(" in tit[0]


def test_titulo_curto_quando_video_e_curto():
    ass = L.montar_ass([], "X", "canal", 2.0, "jogo")
    tit = [l for l in _dialogos(ass) if _tempos(l)[2] == "Titulo"]
    assert _tempos(tit[0])[1] <= 2.0


def test_sem_titulo_nao_gera_evento_de_titulo():
    ass = L.montar_ass([], "  ", "canal", 10.0, "jogo")
    assert not [l for l in _dialogos(ass) if _tempos(l)[2] == "Titulo"]


# ---------- blocos ----------
def test_blocos_respeitam_limites():
    pal = [_pal(w, i * 0.3, i * 0.3 + 0.25) for i, w in enumerate(
        "eu nao acredito que ele fez isso de novo mano olha".split())]
    blocos = L.agrupar_palavras(L.preparar_palavras(pal))
    assert sum(len(b) for b in blocos) == len(pal)
    for b in blocos:
        assert 1 <= len(b) <= L.MAX_PALAVRAS_BLOCO
        if len(b) > 1:
            assert len(" ".join(x["txt"] for x in b)) <= L.MAX_CHARS_BLOCO


def test_bloco_quebra_em_pausa_e_pontuacao():
    pal = [_pal("oi", 0.0, 0.2), _pal("tudo", 2.0, 2.2), _pal("bem.", 2.3, 2.5), _pal("sim", 2.6, 2.8)]
    blocos = L.agrupar_palavras(L.preparar_palavras(pal))
    assert [[x["txt"] for x in b] for b in blocos] == [["OI"], ["TUDO", "BEM"], ["SIM"]]


def test_palavra_longa_sozinha_nao_some():
    pal = [_pal("inconstitucionalissimamente", 0.0, 1.0)]
    blocos = L.agrupar_palavras(L.preparar_palavras(pal))
    assert len(blocos) == 1 and len(blocos[0]) == 1


def test_legenda_caixa_alta_e_palavra_atual_amarela():
    pal = [_pal("olha", 0.0, 0.3), _pal("isso", 0.35, 0.6), _pal("aí", 0.65, 0.9)]
    ass = L.montar_ass(pal, "", "canal", 5.0, "jogo")
    leg = [l for l in _dialogos(ass) if _tempos(l)[2] == "Leg"]
    assert len(leg) == 3                                    # um evento por palavra falada
    for l in leg:
        assert l.count(L.COR_DESTAQUE) == 1                 # só a palavra atual em amarelo
        texto = re.sub(r"\{[^}]*\}", "", l.split(",", 9)[9])
        assert texto == texto.upper() and "OLHA ISSO AÍ" in texto
    # palavra atual muda a cada evento
    atuais = [re.search(re.escape(L.COR_DESTAQUE) + r"[^}]*\}([^{]+)", l).group(1) for l in leg]
    assert atuais == ["OLHA", "ISSO", "AÍ"]


def test_palavras_invalidas_sao_ignoradas():
    pal = [_pal("", 0, 1), {"w": "x"}, _pal("ok", 1.0, 0.5), _pal("ok2", float("nan"), 1.0), _pal("bom", 1.0, 1.4)]
    ass = L.montar_ass(pal, "", "canal", 5.0, "jogo")
    leg = [l for l in _dialogos(ass) if _tempos(l)[2] == "Leg"]
    assert len(leg) == 1 and "BOM" in leg[0]


# ---------- layout, crédito e marca ----------
def test_posicao_da_legenda_depende_do_layout():
    pal = [_pal("teste", 0.0, 0.5)]
    y_cam = int(re.search(r"\\pos\(\d+,(\d+)\)", [l for l in _dialogos(L.montar_ass(pal, "", "c", 3, "cam_jogo"))
                                                  if "Leg" in l][0]).group(1))
    y_jogo = int(re.search(r"\\pos\(\d+,(\d+)\)", [l for l in _dialogos(L.montar_ass(pal, "", "c", 3, "jogo"))
                                                   if "Leg" in l][0]).group(1))
    assert y_cam == L.posicoes("cam_jogo")["legenda"]
    assert L.Y_EMENDA_CAM < y_cam < L.Y_EMENDA_CAM + 300   # logo abaixo da faixa cam/jogo (decisão 2026-11-18)
    assert y_jogo > config.OUT_H // 2                       # abaixo do centro
    assert y_cam != y_jogo
    assert L.posicoes("qualquer") == L.posicoes("jogo")


def test_credito_o_video_todo():
    ass = L.montar_ass([], "", "Criador", 42.0, "jogo")
    cred = [l for l in _dialogos(ass) if _tempos(l)[2] == "Credito"]
    assert len(cred) == 1
    a, b, _ = _tempos(cred[0])
    assert a == 0.0 and b == pytest.approx(42.0, abs=0.01)
    assert "Criador" in cred[0] and L.PREFIXO_CREDITO.strip() in cred[0]


def test_credito_nao_duplica_prefixo():
    ass = L.montar_ass([], "", L.PREFIXO_CREDITO + "Criador", 5.0, "jogo")
    assert ass.count(L.PREFIXO_CREDITO.strip()) == 1


def test_marca_opcional(monkeypatch):
    monkeypatch.setattr(config, "MARCA", "")
    assert not [l for l in _dialogos(L.montar_ass([], "", "c", 5, "jogo")) if _tempos(l)[2] == "Marca"]
    monkeypatch.setattr(config, "MARCA", "@meucanal")
    marca = [l for l in _dialogos(L.montar_ass([], "", "c", 5, "jogo")) if _tempos(l)[2] == "Marca"]
    assert len(marca) == 1 and "@meucanal" in marca[0]


def test_cabecalho_1080x1920_e_fontes():
    ass = L.montar_ass([], "T", "c", 5, "jogo")
    assert f"PlayResX: {config.OUT_W}" in ass and f"PlayResY: {config.OUT_H}" in ass
    assert config.FONTE_LEGENDA in ass and config.FONTE_TITULO in ass


def test_ass_da_capa_tem_titulo_e_credito():
    ass = L.montar_ass_capa("O MAIOR FAIL DA LIVE {\\an1}", "Canal", "cam_jogo")
    linhas = _dialogos(ass)
    assert any("MAIOR FAIL" in l for l in linhas) and any("Canal" in l for l in linhas)
    assert "\\an1" not in ass
