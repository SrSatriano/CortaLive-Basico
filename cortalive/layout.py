"""Detecção do layout da live: tem facecam (rosto fixo e pequeno num canto) ou é só gameplay.

Layout (pixels no vídeo de origem):
  {"tipo": "cam_jogo", "cam": [x, y, w, h], "jogo": [x, y, w, h]}  ou  {"tipo": "jogo", "jogo": [x, y, w, h]}

Amostra alguns frames espalhados do trecho (ffmpeg, reduzidos a ANALISE_LARGURA px), detecta rostos com o YuNet
(cv2.FaceDetectorYN) e procura um rosto que fique na mesma região em boa parte das amostras.
Cache por live em config.CACHE/layout/<live_id>.json, revalidado a cada REVALIDAR_LAYOUT_SEG segundos de live.
"""
import math
import re
import subprocess
import threading
from pathlib import Path

import numpy as np

from cortalive import config
from cortalive.util import ler_json, log, probe, salvar_json

ORIGEM_LOG = "layout"

# ---------- análise ----------
ANALISE_LARGURA = 640              # frames reduzidos para essa largura antes da detecção (rapidez)
AMOSTRAS_PADRAO = 12
AMOSTRAS_MINIMAS = 3               # menos frames lidos que isso → análise não confiável → "jogo"
TIMEOUT_FRAME_SEG = 60             # limite para o ffmpeg extrair um frame

# ---------- YuNet (detector de rosto do OpenCV) ----------
YUNET_LIMIAR_SCORE = 0.72
YUNET_LIMIAR_NMS = 0.3
YUNET_TOP_K = 40
ROSTO_MIN_PX_ANALISE = 10          # rostos menores que isso (na resolução de análise) são ruído

# ---------- critério de facecam ----------
FRACAO_MAX_ROSTO = 0.22            # largura do rosto < 22% da largura do vídeo
FRACAO_MIN_PRESENCA = 0.5          # rosto na mesma região em ≥ 50% das amostras
FATOR_TOLERANCIA_POSICAO = 0.8     # centros a até 0.8× a largura do rosto contam como "mesma região"
# Facecam de live fica num canto/borda. Rosto com o centro no miolo do quadro (cena de "papo", cam em tela
# cheia) não é overlay: cortar a "cam" duplicaria o rosto em cima e embaixo.
FAIXA_CENTRAL_X = (0.30, 0.70)     # fração da largura considerada miolo
FAIXA_CENTRAL_Y = (0.20, 0.80)     # fração da altura considerada miolo

# ---------- retângulo da cam ----------
FATOR_LARGURA_CAM = 2.6            # cam = 2.6× a largura do rosto
PROPORCAO_CAM = 4 / 3              # largura / altura
DESLOC_VERTICAL_CAM = 0.15         # centro da cam um pouco abaixo do rosto (mostra ombros), em alturas de rosto

# ---------- cache ----------
REVALIDAR_LAYOUT_SEG = 30 * 60     # refaz a análise a cada ~30 min de live
PADRAO_LIVE_ID = re.compile(r"^\d{8}_\d{6}$")

_LOCKS = {}
_LOCK_GERAL = threading.Lock()


# =====================================================================
# Funções puras
# =====================================================================

def _rect_valido(r, w_frame):
    """Retângulo [x, y, w, h] numérico, finito, com tamanho positivo e rosto pequeno (< FRACAO_MAX_ROSTO)."""
    try:
        x, y, w, h = (float(v) for v in r[:4])
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (x, y, w, h)) or w <= 0 or h <= 0:
        return None
    if w >= FRACAO_MAX_ROSTO * w_frame:
        return None
    return x, y, w, h


def agrupar_rostos(deteccoes, w, h):
    """Acha a facecam: rosto pequeno que aparece na MESMA região em ≥ FRACAO_MIN_PRESENCA das amostras.

    deteccoes: uma lista por amostra (frame), cada uma com retângulos [x, y, w, h] em pixels do vídeo.
    Devolve o retângulo mediano do rosto [x, y, w, h] (ints) ou None.
    """
    if not deteccoes or w <= 0 or h <= 0:
        return None
    grupos = []  # cada grupo: {"amostras": set, "rects": [(x, y, w, h)]}
    for idx, amostra in enumerate(deteccoes):
        for r in amostra or []:
            rect = _rect_valido(r, w)
            if rect is None:
                continue
            cx, cy = rect[0] + rect[2] / 2, rect[1] + rect[3] / 2
            melhor, melhor_dist = None, None
            for g in grupos:
                arr = np.array(g["rects"])
                gx = float(np.mean(arr[:, 0] + arr[:, 2] / 2))
                gy = float(np.mean(arr[:, 1] + arr[:, 3] / 2))
                tolerancia = FATOR_TOLERANCIA_POSICAO * float(np.median(arr[:, 2]))
                dist = math.hypot(cx - gx, cy - gy)
                if dist <= tolerancia and (melhor_dist is None or dist < melhor_dist):
                    melhor, melhor_dist = g, dist
            if melhor is None:
                grupos.append({"amostras": {idx}, "rects": [rect]})
            else:
                melhor["amostras"].add(idx)
                melhor["rects"].append(rect)
    if not grupos:
        return None
    escolhido = max(grupos, key=lambda g: (len(g["amostras"]), len(g["rects"])))
    if len(escolhido["amostras"]) < FRACAO_MIN_PRESENCA * len(deteccoes):
        return None
    mediana = np.median(np.array(escolhido["rects"]), axis=0)
    return [int(round(v)) for v in mediana]


def _par(v):
    """Maior inteiro par ≤ v (ffmpeg/yuv420p gosta de dimensões pares), no mínimo 2."""
    return max(2, int(v) // 2 * 2)


def expandir_cam(rect, w, h):
    """Retângulo da facecam a partir do rosto: ~FATOR_LARGURA_CAM× a largura, proporção 4:3, preso ao frame."""
    fx, fy, fw, fh = (float(v) for v in rect[:4])
    cam_w = FATOR_LARGURA_CAM * fw
    cam_h = cam_w / PROPORCAO_CAM
    if cam_w > w:
        cam_w, cam_h = w, w / PROPORCAO_CAM
    if cam_h > h:
        cam_h = h
        cam_w = min(w, h * PROPORCAO_CAM)
    cam_w, cam_h = min(_par(cam_w), _par(w)), min(_par(cam_h), _par(h))
    cx = fx + fw / 2
    cy = fy + fh / 2 + DESLOC_VERTICAL_CAM * fh
    x = int(round(min(max(cx - cam_w / 2, 0), w - cam_w)))
    y = int(round(min(max(cy - cam_h / 2, 0), h - cam_h)))
    return [max(0, x), max(0, y), int(cam_w), int(cam_h)]


def eh_overlay_de_cam(rosto, w, h):
    """True se o rosto está fora do miolo do quadro (facecam em canto/borda)."""
    fx, fy, fw, fh = (float(v) for v in rosto[:4])
    cx, cy = (fx + fw / 2) / w, (fy + fh / 2) / h
    no_miolo = FAIXA_CENTRAL_X[0] <= cx <= FAIXA_CENTRAL_X[1] and FAIXA_CENTRAL_Y[0] <= cy <= FAIXA_CENTRAL_Y[1]
    return not no_miolo


def montar_layout(rosto, w, h):
    """Layout final a partir do rosto da facecam (ou None)."""
    jogo = [0, 0, int(w), int(h)]
    if rosto is None or not eh_overlay_de_cam(rosto, w, h):
        return {"tipo": "jogo", "jogo": jogo}
    return {"tipo": "cam_jogo", "cam": expandir_cam(rosto, w, h), "jogo": jogo}


def tempos_amostra(ini, fim, n):
    """n instantes espalhados no meio de fatias iguais de [ini, fim] (evita a borda exata dos blocos)."""
    if n <= 0 or fim <= ini:
        return []
    passo = (fim - ini) / n
    return [ini + (i + 0.5) * passo for i in range(n)]


def _layout_valido(lay):
    if not isinstance(lay, dict) or lay.get("tipo") not in ("jogo", "cam_jogo"):
        return False
    chaves = ["jogo"] + (["cam"] if lay["tipo"] == "cam_jogo" else [])
    return all(isinstance(lay.get(c), list) and len(lay[c]) == 4
               and all(isinstance(v, int) for v in lay[c]) for c in chaves)


# =====================================================================
# Leitura de frames e detecção
# =====================================================================

def _normalizar(segmentos_ou_video):
    """Aceita um vídeo (Path/str) ou lista de Segmento; devolve lista de Segmento ordenada."""
    if isinstance(segmentos_ou_video, (str, Path)):
        info = probe(segmentos_ou_video)
        return [{"arquivo": Path(segmentos_ou_video), "ini": 0.0, "fim": float(info["dur"])}]
    return sorted(segmentos_ou_video, key=lambda s: s["ini"])


def _dimensoes(segmentos):
    info = probe(segmentos[0]["arquivo"])
    return info["w"], info["h"]


def _tamanho_analise(w, h):
    aw = ANALISE_LARGURA
    ah = _par(h * aw / w)
    return aw, ah


def _ler_frame(arquivo, deslocamento, aw, ah):
    """Um frame BGR (ah × aw) em `deslocamento` segundos do arquivo, ou None."""
    cmd = [config.FFMPEG, "-v", "error", "-ss", f"{max(0.0, deslocamento):.3f}", "-i", str(arquivo),
           "-frames:v", "1", "-vf", f"scale={aw}:{ah}", "-pix_fmt", "bgr24", "-f", "rawvideo", "-"]
    r = subprocess.run(cmd, capture_output=True, timeout=TIMEOUT_FRAME_SEG,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    tam = aw * ah * 3
    if r.returncode != 0 or len(r.stdout) < tam:
        erro = r.stderr.decode("utf-8", "replace")[-300:].strip()
        log(f"frame não lido em {Path(arquivo).name} @ {deslocamento:.1f}s: {erro or 'saída curta'}", ORIGEM_LOG)
        return None
    return np.frombuffer(r.stdout[:tam], np.uint8).reshape((ah, aw, 3))


def _criar_detector(aw, ah):
    import cv2
    try:                                   # o OpenCV 5 avisa coisas sem importância no console
        cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
    except AttributeError:
        pass
    det = cv2.FaceDetectorYN.create(str(config.MODELO_ROSTO), "", (aw, ah),
                                    YUNET_LIMIAR_SCORE, YUNET_LIMIAR_NMS, YUNET_TOP_K)
    det.setInputSize((aw, ah))
    return det


def _rostos_nas_amostras(segmentos, tempos, w, h):
    """Para cada instante (segundos de live) com frame legível: lista de rostos [x, y, w, h] em pixels da origem."""
    aw, ah = _tamanho_analise(w, h)
    escala_x, escala_y = w / aw, h / ah
    det = _criar_detector(aw, ah)
    resultado = []
    for t in tempos:
        seg = next((s for s in segmentos if s["ini"] <= t < s["fim"]), None)
        if seg is None:
            continue
        frame = _ler_frame(seg["arquivo"], t - seg["ini"], aw, ah)
        if frame is None:
            continue
        _, faces = det.detect(frame)
        rostos = []
        if faces is not None:
            for fc in faces:
                if fc[2] < ROSTO_MIN_PX_ANALISE:
                    continue
                rostos.append([float(fc[0]) * escala_x, float(fc[1]) * escala_y,
                               float(fc[2]) * escala_x, float(fc[3]) * escala_y])
        resultado.append(rostos)
    return resultado


# =====================================================================
# Cache e função principal
# =====================================================================

def _live_id(segmentos):
    """live_id = nome da pasta da gravação (AAAAMMDD_HHMMSS); None para vídeo avulso."""
    pasta = Path(segmentos[0]["arquivo"]).parent.name
    return pasta if PADRAO_LIVE_ID.match(pasta) else None


def _lock_da(chave):
    with _LOCK_GERAL:
        return _LOCKS.setdefault(chave, threading.Lock())


def _analisar(segmentos, amostras, w, h, ini=None, fim=None):
    """Roda a detecção. Devolve (layout, confiavel). Falha → ("jogo", False) para não gravar no cache."""
    t_ini = segmentos[0]["ini"] if ini is None else max(float(ini), segmentos[0]["ini"])
    t_fim = max(s["fim"] for s in segmentos) if fim is None else min(float(fim), max(s["fim"] for s in segmentos))
    tempos = tempos_amostra(t_ini, t_fim, amostras)
    try:
        deteccoes = _rostos_nas_amostras(segmentos, tempos, w, h)
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as e:
        log(f"análise de rosto falhou, usando só gameplay: {e}", ORIGEM_LOG)
        return montar_layout(None, w, h), False
    except Exception as e:  # noqa: BLE001 - erro do OpenCV (cv2.error) não pode derrubar o corte
        log(f"erro inesperado na detecção de rosto, usando só gameplay: {type(e).__name__}: {e}", ORIGEM_LOG)
        return montar_layout(None, w, h), False
    if len(deteccoes) < min(AMOSTRAS_MINIMAS, len(tempos)) or not deteccoes:
        log(f"só {len(deteccoes)} de {len(tempos)} frames lidos; usando só gameplay", ORIGEM_LOG)
        return montar_layout(None, w, h), False
    rosto = agrupar_rostos(deteccoes, w, h)
    return montar_layout(rosto, w, h), True


def detectar_layout(segmentos_ou_video, amostras=AMOSTRAS_PADRAO, chave_cache=None, ini=None, fim=None):
    """Layout do trecho: facecam + jogo ou só jogo.

    segmentos_ou_video: lista de Segmento (live gravada) ou caminho de um vídeo.
    chave_cache: opcional; por padrão é o live_id (nome da pasta da gravação). Vídeo avulso sem chave → sem cache.
    """
    segmentos = _normalizar(segmentos_ou_video)
    if not segmentos:
        raise ValueError("nenhum segmento para analisar o layout")
    w, h = _dimensoes(segmentos)
    if ini is not None and fim is not None:
        # corte específico: analisa só o trecho dele (a cena muda: papo → menu → jogo), sem cache da live
        return _analisar(segmentos, amostras, w, h, ini, fim)[0]
    chave = chave_cache or _live_id(segmentos)
    if chave is None:
        return _analisar(segmentos, amostras, w, h)[0]

    t_agora = max(s["fim"] for s in segmentos)
    arq = config.CACHE / "layout" / f"{chave}.json"
    with _lock_da(chave):
        cache = ler_json(arq)
        if (isinstance(cache, dict) and _layout_valido(cache.get("layout"))
                and isinstance(cache.get("t_live"), (int, float))
                and cache.get("dimensoes") == [w, h]
                and abs(t_agora - cache["t_live"]) < REVALIDAR_LAYOUT_SEG):
            return cache["layout"]
        lay, confiavel = _analisar(segmentos, amostras, w, h)
        if confiavel:
            salvar_json(arq, {"t_live": t_agora, "dimensoes": [w, h], "layout": lay})
            log(f"{chave}: layout {lay['tipo']}" + (f" cam={lay['cam']}" if "cam" in lay else ""), ORIGEM_LOG)
        return lay
