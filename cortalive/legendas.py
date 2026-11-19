"""Gera o arquivo .ass (1080x1920) do corte: legenda palavra-a-palavra, título de gancho, crédito e marca.

Posições dependem do layout do corte (`cam_jogo` = cam em cima e jogo embaixo; `jogo` = imagem
centralizada sobre fundo desfocado).
"""
import math
import re
import textwrap
import unicodedata

from cortalive import config

W, H = config.OUT_W, config.OUT_H

# ---------- geometria do quadro vertical (compartilhada com o render) ----------
ALTURA_CAM = 640                       # faixa da facecam no topo (layout cam_jogo)
ALTURA_FAIXA = 8                       # faixa fina de separação cam/jogo
ALTURA_JOGO = H - ALTURA_CAM - ALTURA_FAIXA
Y_EMENDA_CAM = ALTURA_CAM + ALTURA_FAIXA // 2
ZOOM_FRENTE = 0.75                     # layout jogo: frente = crop central de 75% da largura do gameplay
PROPORCAO_FONTE_TIPICA = 16 / 9        # gameplay típico (para estimar onde a frente fica no layout jogo)
ALTURA_FRENTE_TIPICA = int(W / (PROPORCAO_FONTE_TIPICA * ZOOM_FRENTE))
Y_FRENTE_TOPO = (H - ALTURA_FRENTE_TIPICA) // 2

# ---------- cores (BGR do ASS) ----------
COR_DESTAQUE = "&H00E5FF&"             # amarelo da palavra atual
COR_BRANCO = "&H00FFFFFF&"
COR_FAIXA_HEX = "0xFFE500"             # mesma cor amarela em RGB (faixa cam/jogo no render)

# ---------- tempos ----------
TITULO_DUR_SEG = 3.0                   # título de gancho aparece só nos primeiros segundos
TITULO_FADE_IN_MS = 120
TITULO_FADE_OUT_MS = 250
TITULO_ESCALA_INI = 60                 # % da escala no início da animação de entrada
TITULO_ESCALA_PICO = 106
TITULO_ANIM_PICO_MS = 140
TITULO_ANIM_FIM_MS = 220
DESTAQUE_ESCALA = 116                  # "pulo" da palavra atual
DESTAQUE_ANIM_MS = 110
BLOCO_ESCALA_INI = 88                  # entrada do bloco novo
BLOCO_ANIM_MS = 90
SOBRA_FIM_BLOCO_SEG = 0.35             # bloco fica mais um pouco na tela depois da última palavra
EVENTO_MIN_SEG = 0.04                  # eventos mais curtos que isso não aparecem
CAPA_DUR_SEG = 10.0                    # duração mínima dos eventos no .ass da capa (só 1 frame é usado)

# ---------- blocos de legenda ----------
MAX_PALAVRAS_BLOCO = 3
MAX_CHARS_BLOCO = 14
PAUSA_QUEBRA_SEG = 0.32                # pausa de fala maior que isso começa bloco novo
PONTUACAO_QUEBRA = ".?!,;:…"
LARGURA_QUEBRA_TITULO = 18             # caracteres por linha do título
LARGURA_QUEBRA_CAPA = 16

# ---------- posições ----------
MARGEM_CREDITO_X = 36
Y_CREDITO = int(H * 0.035)
PREFIXO_CREDITO = "🎮 "
Y_MARCA = int(H * 0.78)
DESCIDA_LEGENDA_CAM = 170              # legenda logo abaixo da faixa amarela, sobre o topo do gameplay
DESCIDA_TITULO_CAM = 470               # título do gancho abaixo da legenda

_POSICOES = {
    "cam_jogo": {"legenda": Y_EMENDA_CAM + DESCIDA_LEGENDA_CAM, "titulo": Y_EMENDA_CAM + DESCIDA_TITULO_CAM,
                 "capa": ALTURA_CAM + ALTURA_FAIXA + ALTURA_JOGO // 2},
    "jogo": {"legenda": int(H * 0.70), "titulo": int(H * 0.155), "capa": H // 2},
}

CAB = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Leg,{config.FONTE_LEGENDA},112,&H00FFFFFF,&H00FFFFFF,&H00000000,&H8C000000,0,0,0,0,100,100,1,0,1,8,4,5,50,50,0,1
Style: Titulo,{config.FONTE_TITULO},100,&H00FFFFFF,&H00FFFFFF,&H00302AE6,&H00000000,0,0,0,0,100,100,2,0,3,17,0,5,50,50,0,1
Style: Capa,{config.FONTE_TITULO},128,&H00FFFFFF,&H00FFFFFF,&H00302AE6,&H00000000,0,0,0,0,100,100,2,0,3,22,0,5,50,50,0,1
Style: Credito,Poppins ExtraBold,40,&H20FFFFFF,&H20FFFFFF,&H64000000,&H00000000,0,0,0,0,100,100,0,0,1,3,1,7,0,0,0,1
Style: Marca,Poppins ExtraBold,46,&H50FFFFFF,&H50FFFFFF,&H90000000,&H00000000,0,0,0,0,100,100,1,0,1,2,0,5,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

_CAMADA = {"Leg": 2, "Titulo": 5, "Capa": 5, "Credito": 8, "Marca": 8}


# ---------- utilitários ----------
def ts(t):
    """Segundos → tempo do ASS (H:MM:SS.cc)."""
    t = max(0.0, float(t))
    cs = int(round(t * 100))
    h, r = divmod(cs, 360000)
    m, r = divmod(r, 6000)
    s, c = divmod(r, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def ts_para_seg(txt):
    """Tempo do ASS (H:MM:SS.cc) → segundos."""
    h, m, s = txt.strip().split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def escapar(txt):
    """Texto vindo da IA/streamer → seguro para o ASS: sem chaves, barras invertidas, quebras ou controles."""
    if not txt:
        return ""
    t = str(txt).replace("{", "").replace("}", "").replace("\\", "")
    t = "".join(c if unicodedata.category(c)[0] != "C" else " " for c in t)
    return re.sub(r"\s+", " ", t).strip()


def _limpa_palavra(w):
    w = escapar(w).upper()
    w = re.sub(r"^[\"“”'(\[]+", "", w)
    return re.sub(r"[\"“”')\].,;:…!?]+$", "", w)


def _quebrar(txt, largura):
    return "\\N".join(textwrap.wrap(txt, largura)) or txt


def _ev(estilo, a, b, txt):
    return f"Dialogue: {_CAMADA[estilo]},{ts(a)},{ts(b)},{estilo},,0,0,0,,{txt}\n"


def _numero(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def posicoes(layout_tipo):
    """Posições verticais (y) dos elementos para o layout; tipo desconhecido usa o de `jogo`."""
    return dict(_POSICOES.get(layout_tipo, _POSICOES["jogo"]))


# ---------- legenda palavra-a-palavra ----------
def preparar_palavras(palavras, dur=None):
    """Valida, limpa (CAIXA ALTA, sem pontuação nas bordas) e ordena; corta no fim do vídeo."""
    saida = []
    for p in palavras or []:
        if not isinstance(p, dict):
            continue
        s, e = _numero(p.get("s")), _numero(p.get("e"))
        txt = _limpa_palavra(p.get("w", ""))
        if s is None or e is None or e < s or not txt:
            continue
        s = max(0.0, s)
        if dur is not None:
            if s >= dur:
                continue
            e = min(e, dur)
        saida.append({"w": str(p["w"]), "s": s, "e": e, "txt": txt})
    return sorted(saida, key=lambda x: x["s"])


def agrupar_palavras(ps):
    """Blocos de até MAX_PALAVRAS_BLOCO palavras / MAX_CHARS_BLOCO caracteres, quebrando em pausa e pontuação."""
    grupos, g = [], []
    for w in ps:
        if g:
            pausa = w["s"] - g[-1]["e"]
            chars = sum(len(x["txt"]) + 1 for x in g) + len(w["txt"])
            if (len(g) >= MAX_PALAVRAS_BLOCO or chars > MAX_CHARS_BLOCO or pausa > PAUSA_QUEBRA_SEG
                    or g[-1]["w"].rstrip()[-1:] in PONTUACAO_QUEBRA):
                grupos.append(g)
                g = []
        g.append(w)
    if g:
        grupos.append(g)
    return grupos


def eventos_legenda(palavras, dur, y):
    """Um evento por palavra falada: o bloco inteiro na tela com a palavra atual em amarelo."""
    grupos = agrupar_palavras(preparar_palavras(palavras, dur))
    out = []
    for gi, g in enumerate(grupos):
        inicio_prox = grupos[gi + 1][0]["s"] if gi + 1 < len(grupos) else dur
        fim_grupo = min(g[-1]["e"] + SOBRA_FIM_BLOCO_SEG, inicio_prox, dur)
        for wi, w in enumerate(g):
            a = w["s"]
            b = g[wi + 1]["s"] if wi + 1 < len(g) else fim_grupo
            b = min(b, dur)
            if b - a < EVENTO_MIN_SEG:
                continue
            partes = []
            for wj, x in enumerate(g):
                if wj == wi:
                    partes.append(f"{{\\c{COR_DESTAQUE}\\fscx{DESTAQUE_ESCALA}\\fscy{DESTAQUE_ESCALA}"
                                  f"\\t(0,{DESTAQUE_ANIM_MS},\\fscx100\\fscy100)}}{x['txt']}{{\\c{COR_BRANCO}}}")
                else:
                    partes.append(x["txt"])
            entrada = (f"{{\\fscx{BLOCO_ESCALA_INI}\\fscy{BLOCO_ESCALA_INI}\\t(0,{BLOCO_ANIM_MS},\\fscx100\\fscy100)}}"
                       if wi == 0 else "")
            out.append(_ev("Leg", a, b, f"{{\\pos({W // 2},{y})}}{entrada}" + " ".join(partes)))
    return out


# ---------- título, crédito e marca ----------
def evento_titulo(titulo, dur, y):
    txt = escapar(titulo).upper()
    fim = min(TITULO_DUR_SEG, dur)
    if not txt or fim <= EVENTO_MIN_SEG:
        return []
    anim = (f"{{\\pos({W // 2},{y})\\fad({TITULO_FADE_IN_MS},{TITULO_FADE_OUT_MS})"
            f"\\fscx{TITULO_ESCALA_INI}\\fscy{TITULO_ESCALA_INI}"
            f"\\t(0,{TITULO_ANIM_PICO_MS},\\fscx{TITULO_ESCALA_PICO}\\fscy{TITULO_ESCALA_PICO})"
            f"\\t({TITULO_ANIM_PICO_MS},{TITULO_ANIM_FIM_MS},\\fscx100\\fscy100)}}")
    return [_ev("Titulo", 0.0, fim, anim + _quebrar(txt, LARGURA_QUEBRA_TITULO))]


def texto_credito(credito):
    txt = escapar(credito)
    if not txt:
        return ""
    prefixo = PREFIXO_CREDITO.strip()
    if txt.startswith(prefixo):
        txt = txt[len(prefixo):].strip()
    return PREFIXO_CREDITO + txt


def evento_credito(credito, dur):
    txt = texto_credito(credito)
    if not txt or dur <= EVENTO_MIN_SEG:
        return []
    return [_ev("Credito", 0.0, dur, f"{{\\an7\\pos({MARGEM_CREDITO_X},{Y_CREDITO})}}{txt}")]


def evento_marca(dur):
    txt = escapar(config.MARCA)
    if not txt or dur <= EVENTO_MIN_SEG:
        return []
    return [_ev("Marca", 0.0, dur, f"{{\\an5\\pos({W // 2},{Y_MARCA})}}{txt}")]


# ---------- montagem ----------
def montar_ass(palavras, titulo_tela, credito, dur, layout_tipo) -> str:
    """Arquivo .ass completo do corte.

    `palavras`: [{"w","s","e"}] em segundos relativos ao INÍCIO DO CORTE (tempo do vídeo de saída).
    `credito`: nome do canal (o prefixo 🎮 é colocado aqui). `dur`: duração do corte em segundos.
    """
    dur = max(0.0, _numero(dur) or 0.0)
    pos = posicoes(layout_tipo)
    eventos = []
    eventos += evento_credito(credito, dur)
    eventos += evento_marca(dur)
    eventos += eventos_legenda(palavras, dur, pos["legenda"])
    eventos += evento_titulo(titulo_tela, dur, pos["titulo"])
    return CAB + "".join(eventos)


def montar_ass_capa(titulo, credito, layout_tipo, dur=CAPA_DUR_SEG) -> str:
    """.ass da capa (.jpg): título grande no meio da área de jogo + crédito, sem animação.

    Os eventos cobrem `dur` segundos inteiros, para o frame da capa poder ser tirado em qualquer instante do corte.
    """
    pos = posicoes(layout_tipo)
    dur = max(CAPA_DUR_SEG, _numero(dur) or 0.0)
    eventos = list(evento_credito(credito, dur))
    txt = escapar(titulo).upper()
    if txt:
        eventos.append(_ev("Capa", 0.0, dur,
                           f"{{\\pos({W // 2},{pos['capa']})}}{_quebrar(txt, LARGURA_QUEBRA_CAPA)}"))
    return CAB + "".join(eventos)
