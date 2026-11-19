"""Render do corte vertical 1080x1920: um único ffmpeg com filter_complex (layout + legendas .ass + áudio).

Saídas para uma base `<base>`: `<base>.mp4`, `<base>.jpg` (capa), `<base>_POST.txt` e `<base>.json`.
O ffmpeg roda com cwd numa pasta temporária do corte, e o filtro `subtitles` usa só nomes relativos
(o .ass e uma cópia das fontes ficam lá dentro) — caminho absoluto do Windows dentro de filtro é frágil.
"""
import itertools
import json
import os
import shutil
import threading
import time
from pathlib import Path

from cortalive import config
from cortalive import legendas as L
from cortalive.trechos import blocos_cobrindo, entrada_ffmpeg
from cortalive.util import log, probe, rodar, salvar_json, slug

ORIGEM_LOG = "render"
W, H = config.OUT_W, config.OUT_H

# geometria (definida em legendas para o texto e o vídeo usarem a mesma)
ALTURA_CAM, ALTURA_FAIXA, ALTURA_JOGO = L.ALTURA_CAM, L.ALTURA_FAIXA, L.ALTURA_JOGO
ZOOM_FRENTE = L.ZOOM_FRENTE

# fundo desfocado do layout jogo (desfoque feito em resolução reduzida: bem mais rápido)
FUNDO_REDUCAO = 4
FUNDO_BLUR_RAIO = 10
FUNDO_BLUR_PASSES = 2
FUNDO_BRILHO = -0.22

# áudio
AUDIO_BITRATE = "160k"
AUDIO_TAXA = 48000
FADE_IN_SEG = 0.15
FADE_OUT_SEG = 0.30
LOUDNORM_TP = -1.5
LOUDNORM_LRA = 11

# capa
CAPA_MARGEM_FIM_SEG = 0.5              # a capa nunca é o último frame (pode faltar)

TOLERANCIA_DUR_SEG = 0.5               # saída mais curta que dur - isso = render falhou
CAPA_QUALIDADE_JPG = 2                 # -q:v do mjpeg (2 = alta)

NOME_MAX_CHARS = 90
SLUG_CANAL_CHARS = 24
SLUG_TITULO_CHARS = 40

_CONTADOR = itertools.count()
_LOCK = threading.Lock()


# ---------- funções puras ----------
def _par(x):
    return max(2, int(x) // 2 * 2)


def _rect(r, w, h):
    """[x,y,w,h] → inteiros pares, dentro do quadro (desloca para dentro em vez de encolher)."""
    if not r or len(r) != 4:
        return 0, 0, _par(w), _par(h)
    rx, ry, rw, rh = (float(v) for v in r)
    rw, rh = min(_par(rw), _par(w)), min(_par(rh), _par(h))
    rx = min(max(0, int(rx)), w - rw) // 2 * 2
    ry = min(max(0, int(ry)), h - rh) // 2 * 2
    return max(0, rx), max(0, ry), rw, rh


def _crop_proporcao(rect, proporcao):
    """Crop central de `rect` com largura/altura = `proporcao`."""
    x, y, w, h = rect
    if w / h > proporcao:
        nw = _par(h * proporcao)
        return x + (w - nw) // 2 // 2 * 2, y, nw, h
    nh = _par(w / proporcao)
    return x, y + (h - nh) // 2 // 2 * 2, w, nh


def _crop(r):
    x, y, w, h = r
    return f"crop={w}:{h}:{x}:{y}"


def montar_filtro(layout, w, h, ass=None, fontsdir=None, capa_ass=None, capa_t=0.0, trim=None) -> str:
    """filter_complex de [0:v] até [v] (1080x1920, fps de saída, yuv420p).

    `ass`/`fontsdir`: nomes RELATIVOS ao cwd do ffmpeg; se dados, queima as legendas no fim.
    `capa_ass`: se dado, gera também a saída [capa] (1 frame em `capa_t` s do corte, pronto para .jpg).
    `trim`: (inicio, duracao) — modo "busca lenta": a entrada vem do começo do bloco e o corte é feito aqui.
    """
    w, h = int(w), int(h)
    tipo = (layout or {}).get("tipo")
    jogo = _rect((layout or {}).get("jogo"), w, h)
    fim = f"fps={config.OUT_FPS},format=yuv420p"
    # o PTS do trecho NÃO começa em 0 (offset do MPEG-TS + busca): o `subtitles` usa o PTS, então zera antes
    zera = "setpts=PTS-STARTPTS"
    if trim:
        zera = f"{zera},trim=start={trim[0]:.3f}:duration={trim[1]:.3f},{zera}"
    if tipo == "cam_jogo" and (layout or {}).get("cam"):
        cam = _rect(layout["cam"], w, h)
        jogo_crop = _crop_proporcao(jogo, W / ALTURA_JOGO)
        partes = [
            f"[0:v]{zera},split=2[c0][j0]",
            f"[c0]{_crop(cam)},scale={W}:{ALTURA_CAM}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={W}:{ALTURA_CAM},setsar=1,pad={W}:{ALTURA_CAM + ALTURA_FAIXA}:0:0:color={L.COR_FAIXA_HEX}[cam]",
            f"[j0]{_crop(jogo_crop)},scale={W}:{ALTURA_JOGO}:flags=lanczos,setsar=1[jogo]",
            f"[cam][jogo]vstack=inputs=2,{fim}[vbase]",
        ]
    else:
        jx, jy, jw, jh = jogo
        fw = _par(jw * ZOOM_FRENTE)
        frente = (jx + (jw - fw) // 2 // 2 * 2, jy, fw, jh)
        wr, hr = W // FUNDO_REDUCAO, H // FUNDO_REDUCAO
        partes = [
            f"[0:v]{zera},{_crop(jogo)},split=2[f0][b0]",
            f"[b0]scale={wr}:{hr}:force_original_aspect_ratio=increase,crop={wr}:{hr},"
            f"boxblur={FUNDO_BLUR_RAIO}:{FUNDO_BLUR_PASSES},scale={W}:{H},eq=brightness={FUNDO_BRILHO},setsar=1[fundo]",
            f"[f0]crop={frente[2]}:{frente[3]}:{frente[0] - jx}:{frente[1] - jy},"
            f"scale={W}:{H}:force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos,setsar=1[frente]",
            f"[fundo][frente]overlay=(W-w)/2:(H-h)/2,{fim}[vbase]",
        ]
    extra = f":fontsdir={fontsdir}" if fontsdir else ""
    origem = "[vbase]"
    if capa_ass:
        # ramo da capa no MESMO ffmpeg: 1 frame no instante capa_t (tempo do corte) com o .ass da capa
        partes.append("[vbase]split=2[vs][vc]")
        partes.append(f"[vc]select='gte(t,{max(0.0, float(capa_t)):.3f})',trim=end_frame=1,"
                      f"subtitles=f={capa_ass}{extra},scale=out_range=full,format=yuvj420p[capa]")
        origem = "[vs]"
    if ass:
        partes.append(f"{origem}subtitles=f={ass}{extra}[v]")
    elif capa_ass:
        partes.append(f"{origem}null[v]")
    else:
        partes[-1] = partes[-1][:-len("[vbase]")] + "[v]"
    return ";".join(partes)


def nome_base(live, corte) -> str:
    """Nome de arquivo seguro e determinístico: plataforma_canal_HHhMMmSSs_TITULO."""
    seg = max(0, int(float(corte.get("ini", 0))))
    h, r = divmod(seg, 3600)
    quando = f"{h:02d}h{r // 60:02d}m{r % 60:02d}s"
    canal = slug(live.get("canal") or live.get("canal_id") or "canal", SLUG_CANAL_CHARS)
    titulo = slug(corte.get("titulo") or "corte", SLUG_TITULO_CHARS)
    plataforma = slug(live.get("plataforma") or "live", SLUG_CANAL_CHARS)
    return f"{plataforma}_{canal}_{quando}_{titulo}"[:NOME_MAX_CHARS].rstrip("_")


def palavras_no_corte(palavras, origem, ini, dur):
    """Palavras com tempos relativos a `origem` (s de live) → relativos ao início do corte `ini`, dentro de [0, dur]."""
    desloc = float(origem) - float(ini)
    saida = []
    for p in palavras or []:
        try:
            s, e = float(p["s"]) + desloc, float(p["e"]) + desloc
        except (KeyError, TypeError, ValueError):
            continue
        if e <= 0 or s >= dur:
            continue
        saida.append({"w": p.get("w", ""), "s": max(0.0, s), "e": min(float(dur), e)})
    return saida


def texto_post(live, corte) -> str:
    """título\\n\\nlegenda_post (com crédito garantido)\\n\\nhashtags."""
    legenda = (corte.get("legenda_post") or "").strip()
    if "créditos" not in legenda.lower():
        canal, plataforma = live.get("canal") or "criador original", live.get("plataforma") or ""
        credito = f"Créditos: {canal} ({plataforma})" if plataforma else f"Créditos: {canal}"
        legenda = f"{legenda}\n\n{credito}".strip()
    tags = corte.get("hashtags") or config.HASHTAGS_FIXAS.split()
    return f"{corte.get('titulo', '').strip()}\n\n{legenda}\n\n{' '.join(tags)}\n"


# ---------- ffmpeg ----------
def _args_audio(dur, tem_audio, trim=None):
    if not tem_audio:
        return ["-map", "1:a"]
    inicio_fade_out = max(0.0, dur - FADE_OUT_SEG)
    corte = f"atrim=start={trim[0]:.3f}:duration={trim[1]:.3f},asetpts=PTS-STARTPTS," if trim else ""
    af = (f"asetpts=PTS-STARTPTS,{corte}loudnorm=I={config.LOUDNESS_LUFS}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA},"
          f"afade=t=in:st=0:d={FADE_IN_SEG},afade=t=out:st={inicio_fade_out:.3f}:d={FADE_OUT_SEG},"
          f"aresample={AUDIO_TAXA}")
    return ["-map", "0:a:0", "-af", af]


def _pasta_temporaria(base_nome):
    with _LOCK:
        n = next(_CONTADOR)
    pasta = config.TEMP / f"render_{slug(base_nome, SLUG_CANAL_CHARS)}_{threading.get_ident()}_{n}"
    pasta.mkdir(parents=True, exist_ok=True)
    fontes = pasta / "fontes"
    fontes.mkdir(exist_ok=True)
    for f in Path(config.FONTES).glob("*.tt[fc]"):
        shutil.copy2(f, fontes / f.name)
    for f in Path(config.FONTES).glob("*.otf"):
        shutil.copy2(f, fontes / f.name)
    return pasta


def _info_fonte(segmentos, ini, fim):
    blocos = blocos_cobrindo(segmentos, ini, fim)
    if not blocos:
        raise ValueError(f"nenhum bloco cobre o trecho {ini:.1f}–{fim:.1f}")
    return probe(blocos[0]["arquivo"])


def _sem_busca(args):
    """Tira `-ss X -t D` dos args de entrada (modo busca lenta). Devolve (args, X, D)."""
    saida, inicio, duracao, i = [], 0.0, None, 0
    while i < len(args):
        if args[i] == "-ss" and i + 1 < len(args):
            inicio, i = float(args[i + 1]), i + 2
        elif args[i] == "-t" and i + 1 < len(args) and duracao is None:
            duracao, i = float(args[i + 1]), i + 2
        else:
            saida.append(args[i])
            i += 1
    return saida, inicio, duracao


def _saida_valida(mp4, dur):
    """O ffmpeg pode sair com código 0 sem vídeo (ex.: busca impossível no concat): confere com ffprobe."""
    if not Path(mp4).exists() or Path(mp4).stat().st_size == 0:
        return False
    try:
        info = probe(mp4)
    except RuntimeError as e:
        log(f"saída sem vídeo: {e}", ORIGEM_LOG)
        return False
    return info["dur"] >= dur - TOLERANCIA_DUR_SEG


def _render_video(segmentos, ini, fim, layout, montar_textos, pasta, saida_mp4, saida_jpg):
    """Um ffmpeg só: vídeo vertical com legendas + áudio tratado + capa (.jpg) no instante do pico.

    `montar_textos(dur) -> (ass_corte, ass_capa, capa_t)`: chamado com a duração real do trecho.
    Tenta com busca rápida (-ss na entrada); se a saída vier vazia/curta, refaz decodificando desde o
    começo do bloco e cortando com trim no filtro (mais lento, mas não depende de keyframe).
    """
    info = _info_fonte(segmentos, ini, fim)
    args, dur, lista = entrada_ffmpeg(segmentos, ini, fim)
    try:
        ass_corte, ass_capa, capa_t = montar_textos(dur)
        (pasta / "corte.ass").write_text(ass_corte, encoding="utf-8")
        (pasta / "capa.ass").write_text(ass_capa, encoding="utf-8")
        silencio = [] if info["audio"] else ["-f", "lavfi", "-t", f"{dur:.3f}", "-i",
                                             f"anullsrc=r={AUDIO_TAXA}:cl=stereo"]
        tentativas = [(args, None)]
        args_lentos, inicio, _ = _sem_busca(args)
        tentativas.append((args_lentos, (inicio, dur)))
        for n, (entrada, trim) in enumerate(tentativas):
            filtro = montar_filtro(layout, info["w"], info["h"], ass="corte.ass", fontsdir="fontes",
                                   capa_ass="capa.ass", capa_t=capa_t, trim=trim)
            cmd = [config.FFMPEG, "-y", "-v", "error", *entrada, *silencio, "-filter_complex", filtro,
                   # saída 1: o corte
                   "-map", "[v]", *_args_audio(dur, info["audio"], trim),
                   "-c:v", "libx264", "-preset", config.X264_PRESET, "-crf", str(config.CRF),
                   "-profile:v", "high", "-pix_fmt", "yuv420p", "-threads", str(config.FFMPEG_THREADS),
                   "-c:a", "aac", "-b:a", AUDIO_BITRATE, "-ar", str(AUDIO_TAXA),
                   "-t", f"{dur:.3f}", "-movflags", "+faststart", str(saida_mp4),
                   # saída 2: a capa
                   "-map", "[capa]", "-frames:v", "1", "-q:v", str(CAPA_QUALIDADE_JPG), "-update", "1",
                   str(saida_jpg)]
            rodar(cmd, cwd=str(pasta))
            if _saida_valida(saida_mp4, dur):
                break
            if n + 1 < len(tentativas):
                log(f"busca rápida falhou no trecho {ini:.1f}–{fim:.1f}; refazendo com busca lenta", ORIGEM_LOG)
        else:
            raise RuntimeError(f"ffmpeg não gerou o vídeo do corte {ini:.1f}–{fim:.1f}")
    finally:
        lista.unlink(missing_ok=True)
    return dur, info


def _capa_do_mp4(mp4, t, saida):
    """Plano B da capa: um frame do próprio corte."""
    rodar([config.FFMPEG, "-y", "-v", "error", "-ss", f"{max(0.0, t):.3f}", "-i", str(mp4),
           "-frames:v", "1", "-q:v", str(CAPA_QUALIDADE_JPG), "-update", "1", str(saida)])


def _apagar_pasta(pasta):
    def _erro(_func, caminho, exc_info):
        log(f"não consegui apagar {caminho}: {exc_info[1]}", ORIGEM_LOG)
    shutil.rmtree(pasta, onerror=_erro)


def _json_seguro(x):
    return json.loads(json.dumps(x, ensure_ascii=False, default=str))


# ---------- principal ----------
def renderizar(segmentos, corte, layout, palavras, live, destino_base: Path, origem_palavras=None) -> dict:
    """Gera <base>.mp4, <base>.jpg, <base>_POST.txt e <base>.json. Devolve {"mp4","jpg","post","json"}.

    `destino_base`: caminho base sem extensão (ou uma pasta existente → usa `nome_base(live, corte)` dentro).
    `palavras`: tempos relativos a `origem_palavras` (segundos de live); padrão = `corte["ini"]`.
    """
    ini, fim = float(corte["ini"]), float(corte["fim"])
    if fim <= ini:
        raise ValueError(f"corte com duração inválida: {ini}–{fim}")
    destino_base = Path(destino_base).resolve()        # o ffmpeg roda com cwd na pasta temporária
    base = destino_base / nome_base(live, corte) if destino_base.is_dir() else destino_base
    base.parent.mkdir(parents=True, exist_ok=True)
    saidas = {"mp4": base.with_name(base.name + ".mp4"), "jpg": base.with_name(base.name + ".jpg"),
              "post": base.with_name(base.name + "_POST.txt"), "json": base.with_name(base.name + ".json")}
    origem = ini if origem_palavras is None else float(origem_palavras)
    layout_tipo = (layout or {}).get("tipo", "jogo")
    t0 = time.time()

    titulo_tela = corte.get("texto_tela") or corte.get("titulo", "")
    titulo_capa = corte.get("titulo") or corte.get("texto_tela", "")
    canal = live.get("canal", "")
    pal = []

    def montar_textos(dur):
        pal[:] = palavras_no_corte(palavras, origem, ini, dur)
        capa_t = min(max(0.0, float(corte.get("pico", ini)) - ini), max(0.0, dur - CAPA_MARGEM_FIM_SEG))
        return (L.montar_ass(pal, titulo_tela, canal, dur, layout_tipo),
                L.montar_ass_capa(titulo_capa, canal, layout_tipo, dur), capa_t)

    pasta = _pasta_temporaria(base.name)
    tmp_mp4 = base.with_name(base.name + ".tmp.mp4")
    try:
        dur, _ = _render_video(segmentos, ini, fim, layout, montar_textos, pasta, tmp_mp4, saidas["jpg"])
        os.replace(tmp_mp4, saidas["mp4"])
    finally:
        tmp_mp4.unlink(missing_ok=True)
        _apagar_pasta(pasta)
    if not saidas["jpg"].exists() or saidas["jpg"].stat().st_size == 0:
        log("capa não saiu junto com o vídeo; usando um frame do corte", ORIGEM_LOG)
        _capa_do_mp4(saidas["mp4"], float(corte.get("pico", ini)) - ini, saidas["jpg"])

    saidas["post"].write_text(texto_post(live, corte), encoding="utf-8")
    salvar_json(saidas["json"], _json_seguro({
        "corte": corte, "live": live, "layout": layout, "duracao": round(dur, 3),
        "origem_palavras": origem, "texto": " ".join(p["w"] for p in pal), "palavras": pal,
        "arquivos": {k: v.name for k, v in saidas.items()},
        "gerado_em": time.strftime("%Y-%m-%dT%H:%M:%S"), "render_seg": round(time.time() - t0, 1),
    }))
    log(f"corte pronto: {saidas['mp4'].name} ({dur:.1f}s, {layout_tipo}) em {time.time() - t0:.1f}s", ORIGEM_LOG)
    return saidas
