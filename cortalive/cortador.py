"""Pipeline do CortaLive Básico: vídeo local (ou pasta de blocos .ts) → cortes verticais com legenda.

1. O vídeo é dividido em janelas de SEGMENTO_SEG. A janela do bloco N cobre [bloco N-1, bloco N] mas só aceita
   picos da sua "zona" [meio do bloco N-1, meio do bloco N): as zonas são contíguas e não se sobrepõem, então
   nenhum momento se perde na emenda nem sai duas vezes, e todo pico tem contexto dos dois lados.
2. Em cada janela: picos de áudio (detector) → transcrição do candidato ± margem (Whisper local) →
   bordas em pausas de fala → a IA escolhe e escreve título/legenda (curador; sem IA, escolhe pelo volume).
3. No fim, os escolhidos de todas as janelas são ordenados pela nota e os N melhores viram vídeo (render).

O progresso fica em dados/estado/<vídeo>.json: rodar de novo retoma de onde parou (`--refazer` recomeça).
"""
import hashlib
import json
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from cortalive import config, curador, detector, layout, render, transcrever, trechos, util
from cortalive.util import ler_json, log, mmss, salvar_json, slug

ORIGEM_LOG = "cortalive"

# ---------- janelas ----------
SOBREPOSICAO_BLOCOS = 1               # a janela do bloco N inclui o bloco N-1 (momento na emenda não se perde)
ID_JANELA_FINAL = "fim"               # janela extra: cobre a 2ª metade do último bloco
FRACAO_MEIO_BLOCO = 0.5
EPS_TEMPO_SEG = 1e-3
PREFIXO_BLOCO_VIRTUAL = "v"           # nomes dos blocos virtuais de um vídeo único (v00000, v00001…)
EXTENSOES_VIDEO = {".mp4", ".mkv", ".ts", ".mov", ".webm", ".flv", ".m4v"}

# ---------- análise ----------
PASSO_ANALISE_SEG = 0.5               # resolução do áudio no detector
FATOR_CANDIDATOS_EXTRA = 2            # pede mais candidatos ao detector porque a zona filtra parte deles
MARGEM_TRANSCRICAO_SEG = 10.0         # transcreve o candidato ± isto (a IA pode mexer nas bordas aí dentro)
FRACAO_SOBREPOSICAO_DEDUP = 0.5       # sobreposição (do menor) acima disto = mesmo momento

# ---------- tentativas ----------
JANELA_MAX_TENTATIVAS = 2             # janela que deu erro inesperado é tentada de novo 1 vez
JANELA_MAX_TENTATIVAS_TEMPORARIO = 3  # erro temporário (modelo baixando, ffmpeg/áudio falhou): mais paciência
ESPERA_TEMPORARIO_BASE_SEG = 10.0     # espera crescente entre tentativas: base × nº da tentativa

VERSAO_ESTADO = 1
CASAS_TEMPO = 2
NOTA_PADRAO = 0.0
TAM_HASH_CHAVE = 8
_RE_NUMEROS = re.compile(r"(\d+)")


class ErroTemporario(RuntimeError):
    """Falha que tende a passar (modelo do Whisper baixando, ffmpeg sem ler o áudio): a janela é tentada de novo."""


# =====================================================================================  funções puras

def nome_bloco(bloco) -> str:
    """Identificador do bloco: `nome` (blocos virtuais) ou o nome do arquivo .ts."""
    return str(bloco.get("nome") or Path(bloco["arquivo"]).name)


def _meio(bloco):
    return float(bloco["ini"]) + FRACAO_MEIO_BLOCO * (float(bloco["fim"]) - float(bloco["ini"]))


def planejar_janelas(blocos) -> list:
    """Janelas de análise para os blocos.

    Janela do bloco k: tempos [ini(k-1), fim(k)], zona [meio(k-1), meio(k)) (a 1ª começa em ini(0)), mais a janela
    final (zona [meio(último), fim(último)]).
    Cada janela: {"id", "ini", "fim", "zona_ini", "zona_fim", "blocos": [nomes]}.
    """
    ordenados = sorted(blocos or [], key=lambda b: float(b["ini"]))
    janelas = []
    for k, bloco in enumerate(ordenados):
        incluidos = ordenados[max(0, k - SOBREPOSICAO_BLOCOS):k] + [bloco]
        janelas.append({
            "id": nome_bloco(bloco),
            "ini": float(incluidos[0]["ini"]),
            "fim": float(bloco["fim"]),
            "zona_ini": _meio(ordenados[k - 1]) if k > 0 else float(bloco["ini"]),
            "zona_fim": _meio(bloco),
            "blocos": [nome_bloco(b) for b in incluidos],
        })
    if ordenados:
        ultimo = ordenados[-1]
        janelas.append({
            "id": ID_JANELA_FINAL,
            "ini": float(ultimo["ini"]),
            "fim": float(ultimo["fim"]),
            "zona_ini": _meio(ultimo),
            "zona_fim": float(ultimo["fim"]) + EPS_TEMPO_SEG,
            "blocos": [nome_bloco(ultimo)],
        })
    return janelas


SOBRA_MINIMA_SEG = 5.0       # modo VOD: final menor que isto não vira bloco próprio (não teria áudio)


def blocos_virtuais(dur, segmento_seg, arquivo) -> list:
    """Divide um vídeo único em blocos virtuais de `segmento_seg` s, todos apontando para o arquivo."""
    dur = float(dur or 0)
    passo = float(segmento_seg)
    if dur <= 0 or passo <= 0:
        return []
    blocos = []
    k = 0
    while k * passo < dur:
        if blocos and dur - k * passo < SOBRA_MINIMA_SEG:      # sobra de poucos segundos: junta no bloco anterior
            blocos[-1]["fim"] = dur
            break
        blocos.append({"arquivo": Path(arquivo), "ini": k * passo, "fim": min((k + 1) * passo, dur),
                       "nome": f"{PREFIXO_BLOCO_VIRTUAL}{k:05d}"})
        k += 1
    return blocos


def na_zona(cand, janela) -> bool:
    try:
        pico = float(cand["pico"])
    except (KeyError, TypeError, ValueError):
        return False
    return math.isfinite(pico) and float(janela["zona_ini"]) <= pico < float(janela["zona_fim"])


def ja_cortado(intervalo, cortes) -> bool:
    """True se o momento já virou corte: pico dentro de um corte ou sobreposição grande com ele."""
    try:
        ini, fim = float(intervalo["ini"]), float(intervalo["fim"])
        pico = float(intervalo.get("pico", (ini + fim) / 2))
    except (KeyError, TypeError, ValueError):
        return False
    for c in cortes or []:
        try:
            c_ini, c_fim = float(c["ini"]), float(c["fim"])
        except (KeyError, TypeError, ValueError):
            continue
        if c_ini <= pico <= c_fim:
            return True
        comum = min(fim, c_fim) - max(ini, c_ini)
        menor = min(fim - ini, c_fim - c_ini)
        if menor > 0 and comum / menor >= FRACAO_SOBREPOSICAO_DEDUP:
            return True
    return False


def chave_transcricao(chave, ini, fim) -> str:
    return f"{chave}_{ini:.{CASAS_TEMPO}f}_{fim:.{CASAS_TEMPO}f}"


def prender_ao_trecho(cand, segmentos):
    """Prende ini/fim do candidato ao trecho contínuo (sem buraco entre blocos) que contém o pico.

    Mantém a duração deslocando a janela quando dá; pico num buraco ou trecho curto demais → None.
    """
    try:
        pico, ini, fim = float(cand["pico"]), float(cand["ini"]), float(cand["fim"])
    except (KeyError, TypeError, ValueError):
        return None
    trecho = trechos.trecho_continuo_em(segmentos, pico)
    if trecho is None:
        return None
    lim_ini, lim_fim = trecho
    if lim_ini <= ini and fim <= lim_fim:
        return dict(cand)
    if ini < lim_ini:
        fim += lim_ini - ini
        ini = lim_ini
    if fim > lim_fim:
        ini -= fim - lim_fim
        fim = lim_fim
    ini = max(ini, lim_ini)
    if fim - ini < config.CORTE_MIN_SEG - EPS_TEMPO_SEG:
        return None
    return {**cand, "ini": round(ini, 3), "fim": round(fim, 3)}


def selecionar_cortes(escolhidos, ja_feitos, limite) -> list:
    """Melhores momentos (maior nota primeiro) que ainda não viraram corte, sem repetir momento.

    `ja_feitos` contam para o limite e para a deduplicação. `limite` 0/None = sem limite.
    """
    aceitos = list(ja_feitos or [])
    saida = []
    ordem = sorted(escolhidos or [], key=lambda c: (-float(c.get("nota") or NOTA_PADRAO),
                                                    -float(c.get("score") or 0.0), float(c.get("ini") or 0.0)))
    for c in ordem:
        if limite and len(aceitos) >= limite:
            break
        if ja_cortado(c, aceitos):
            continue
        aceitos.append(c)
        saida.append(c)
    return saida


def ordenar_natural(caminhos) -> list:
    """Ordena nomes com números como gente: bloco2 antes de bloco10."""
    def chave(p):
        return [int(x) if x.isdigit() else x.lower() for x in _RE_NUMEROS.split(Path(p).name)]
    return sorted(caminhos, key=chave)


def chave_do_caminho(caminho) -> str:
    """Chave estável do vídeo: nome legível + hash do caminho completo (vídeos com o mesmo nome não colidem)."""
    p = Path(caminho).resolve()
    h = hashlib.sha1(str(p).lower().encode("utf-8")).hexdigest()[:TAM_HASH_CHAVE]
    return f"{slug(p.stem if p.is_file() else p.name, 50)}_{h}"


def pasta_saida_padrao(caminho) -> Path:
    """`saida` ao lado do vídeo (ou dentro da pasta de blocos)."""
    p = Path(caminho)
    return (p if p.is_dir() else p.parent) / config.NOME_PASTA_SAIDA


# =====================================================================================  progresso salvo

class Estado:
    """Progresso de um vídeo (janelas analisadas, momentos escolhidos, cortes prontos). Seguro entre threads.

    Falha de disco ao salvar não derruba o processamento: o estado continua em memória e vai na próxima escrita.
    """

    def __init__(self, caminho):
        self.caminho = Path(caminho)
        self._lock = threading.RLock()
        dados = ler_json(self.caminho, None)
        if not isinstance(dados, dict) or not isinstance(dados.get("janelas"), dict):
            if self.caminho.exists():
                log(f"progresso ilegível em {self.caminho.name}; começando do zero", ORIGEM_LOG)
            dados = self._vazio()
        dados.setdefault("escolhidos", [])
        dados.setdefault("cortes", [])
        self._dados = dados

    @staticmethod
    def _vazio():
        return {"versao": VERSAO_ESTADO, "janelas": {}, "escolhidos": [], "cortes": []}

    def salvar(self) -> bool:
        with self._lock:
            try:
                salvar_json(self.caminho, self._dados)
                return True
            except OSError as e:
                log(f"não consegui salvar o progresso ({type(e).__name__}: {e}); sigo em memória", ORIGEM_LOG)
                return False

    def janela_feita(self, janela_id) -> bool:
        with self._lock:
            return bool((self._dados["janelas"].get(janela_id) or {}).get("feita"))

    def marcar_janela(self, janela_id, escolhidos=(), erro=None):
        with self._lock:
            atual = self._dados["janelas"].get(janela_id) or {}
            if atual.get("feita"):
                return
            self._dados["janelas"][janela_id] = {**atual, "feita": True, "t": time.time(), "erro": erro}
            self._dados["escolhidos"].extend(json.loads(json.dumps(list(escolhidos), default=str)))
            self.salvar()

    def falha_janela(self, janela_id, campo="tentativas") -> int:
        with self._lock:
            j = self._dados["janelas"].setdefault(janela_id, {})
            j[campo] = int(j.get(campo) or 0) + 1
            self.salvar()
            return j[campo]

    def escolhidos(self) -> list:
        with self._lock:
            return [dict(c) for c in self._dados["escolhidos"]]

    def cortes(self) -> list:
        with self._lock:
            return [dict(c) for c in self._dados["cortes"]]

    def registrar_corte(self, registro):
        with self._lock:
            self._dados["cortes"].append(dict(registro))
            self.salvar()

    def esquecer(self):
        with self._lock:
            self._dados = self._vazio()
            self.salvar()


# =====================================================================================  fontes de vídeo

class _Fonte:
    """Base: um vídeo com blocos (reais ou virtuais), áudio analisado uma vez só e os dados para os créditos."""

    def __init__(self, caminho, canal=None, plataforma=""):
        self.caminho = Path(caminho)
        self.chave = chave_do_caminho(self.caminho)
        nome = (canal or "").strip() or self.caminho.stem
        self.live = {"plataforma": (plataforma or "").strip(), "canal": nome, "titulo": self.caminho.stem}
        self._lock = threading.Lock()
        self._audio = None

    def segmentos(self) -> list:
        raise NotImplementedError

    def blocos(self) -> list:
        raise NotImplementedError

    @property
    def duracao(self) -> float:
        segs = self.segmentos()
        return max((float(s["fim"]) for s in segs), default=0.0)

    def energia(self, ini, fim):
        with self._lock:
            if self._audio is None or len(self._audio[0]) == 0:   # leitura que falhou é refeita
                log(f"lendo o áudio de {self.caminho.name} ({mmss(self.duracao)})…", ORIGEM_LOG)
                self._audio = detector.energia_audio(self.segmentos(), PASSO_ANALISE_SEG)
        tempos, db = self._audio
        mascara = (tempos >= ini) & (tempos < fim)
        return tempos[mascara], db[mascara]

    def nome_janela(self, janela) -> str:
        return f"{self.caminho.name} {mmss(janela['ini'])}–{mmss(janela['fim'])}"


class FonteVideo(_Fonte):
    """Um arquivo de vídeo (MP4/MKV/TS…), analisado em blocos virtuais."""

    def __init__(self, arquivo, canal=None, plataforma=""):
        super().__init__(arquivo, canal, plataforma)
        info = util.probe(self.caminho)
        self.dur = float(info.get("dur") or 0)
        if self.dur <= 0:
            raise ValueError(f"não consegui ler a duração de {self.caminho}")

    def segmentos(self) -> list:
        return [{"arquivo": self.caminho, "ini": 0.0, "fim": self.dur}]

    def blocos(self) -> list:
        return blocos_virtuais(self.dur, config.SEGMENTO_SEG, self.caminho)


class FontePasta(_Fonte):
    """Uma pasta com os blocos .ts de uma live gravada, em ordem de nome (bloco2 antes de bloco10)."""

    def __init__(self, pasta, canal=None, plataforma=""):
        super().__init__(pasta, canal, plataforma)
        arquivos = ordenar_natural([p for p in self.caminho.iterdir() if p.is_file() and p.suffix.lower() == ".ts"])
        if not arquivos:
            raise FileNotFoundError(f"a pasta {self.caminho} não tem blocos .ts")
        self._segs = []
        t = 0.0
        for arq in arquivos:
            try:
                dur = float(util.probe(arq).get("dur") or 0)
            except (RuntimeError, OSError, ValueError, KeyError) as e:
                log(f"bloco ilegível ignorado: {arq.name} ({str(e).splitlines()[0][:120]})", ORIGEM_LOG)
                continue
            if dur <= 0:
                continue
            self._segs.append({"arquivo": arq, "ini": round(t, 3), "fim": round(t + dur, 3), "nome": arq.name})
            t += dur
        if not self._segs:
            raise ValueError(f"nenhum bloco .ts legível em {self.caminho}")

    def segmentos(self) -> list:
        return [dict(s) for s in self._segs]

    def blocos(self) -> list:
        return self.segmentos()


def fonte_de_caminho(caminho, canal=None, plataforma=""):
    p = Path(caminho)
    if p.is_dir():
        return FontePasta(p, canal=canal, plataforma=plataforma)
    if p.is_file():
        if p.suffix.lower() not in EXTENSOES_VIDEO:
            log(f"extensão {p.suffix or '(nenhuma)'} incomum; tentando mesmo assim", ORIGEM_LOG)
        return FonteVideo(p, canal=canal, plataforma=plataforma)
    raise FileNotFoundError(f"não encontrei {p}")


# =====================================================================================  uma janela

def _layout_seguro(segs, ini, fim):
    try:
        return layout.detectar_layout(segs, ini=ini, fim=fim)
    except Exception as e:  # noqa: BLE001 - sem layout o corte ainda sai (imagem centralizada)
        log(f"layout falhou ({type(e).__name__}: {e}); usando a imagem inteira", ORIGEM_LOG)
    info = util.probe(segs[0]["arquivo"])
    return {"tipo": "jogo", "jogo": [0, 0, int(info["w"]), int(info["h"])]}


def _transcrever_candidatos(fonte, cands, segs):
    """Transcreve cada candidato ± margem, sem atravessar buraco entre blocos. Devolve (com_texto, falhas)."""
    com_texto, falhas = [], 0
    for c in cands:
        trecho = trechos.trecho_continuo_em(segs, float(c["pico"]))
        lim_ini, lim_fim = trecho if trecho else (min(float(s["ini"]) for s in segs),
                                                  max(float(s["fim"]) for s in segs))
        t_ini = max(lim_ini, float(c["ini"]) - MARGEM_TRANSCRICAO_SEG)
        t_fim = min(lim_fim, float(c["fim"]) + MARGEM_TRANSCRICAO_SEG)
        try:
            tr = transcrever.transcrever_trecho(segs, t_ini, t_fim, chave_transcricao(fonte.chave, t_ini, t_fim))
        except Exception as e:  # noqa: BLE001 - whisper/ffmpeg com problema num trecho não para os outros
            falhas += 1
            log(f"transcrição de {mmss(t_ini)}–{mmss(t_fim)} falhou: {type(e).__name__}: {e}", ORIGEM_LOG)
            continue
        ajustado = detector.ajustar_bordas(c, tr["palavras"], inicio_trecho=t_ini)
        com_texto.append({**ajustado, "trans_ini": t_ini, "trans_fim": t_fim, "palavras": tr["palavras"],
                          "texto": tr.get("texto", "")})
    return com_texto, falhas


def _sem_audio(fonte, janela):
    """Energia veio vazia. Vídeo sem trilha de áudio → nada a fazer; senão é falha (temporária) da leitura."""
    segs = trechos.blocos_cobrindo(fonte.segmentos(), janela["ini"], janela["fim"])
    if not segs:
        return
    try:
        info = util.probe(segs[0]["arquivo"])
    except (RuntimeError, OSError, ValueError, KeyError) as e:
        raise ErroTemporario(f"áudio ilegível ({type(e).__name__}: {str(e)[:200]})") from e
    if not info.get("audio"):
        log(f"{fonte.nome_janela(janela)}: vídeo sem som; nada a analisar", ORIGEM_LOG)
        return
    raise ErroTemporario("a leitura do áudio não devolveu nada")


def processar_janela(fonte, janela) -> list:
    """Analisa uma janela e devolve os momentos escolhidos (corte + palavras para a legenda); pode ser [].

    Levanta ErroTemporario quando nada pôde ser analisado por uma falha que tende a passar.
    """
    tempos, db = fonte.energia(janela["ini"], janela["fim"])
    if len(tempos) == 0:
        _sem_audio(fonte, janela)
        return []
    tempos, db = detector.grade_uniforme(tempos, db, PASSO_ANALISE_SEG)
    brutos = detector.candidatos(tempos, db, max_n=config.MAX_CANDIDATOS_POR_JANELA * FATOR_CANDIDATOS_EXTRA)
    segs = fonte.segmentos()
    cands = []
    for c in brutos:
        if not na_zona(c, janela):
            continue
        preso = prender_ao_trecho(c, segs)
        if preso is not None:
            cands.append(preso)
    cands = cands[:config.MAX_CANDIDATOS_POR_JANELA]
    if not cands:
        log(f"{fonte.nome_janela(janela)}: nenhum momento forte", ORIGEM_LOG)
        return []
    com_texto, falhas = _transcrever_candidatos(fonte, cands, segs)
    if not com_texto:
        if falhas:
            raise ErroTemporario(f"todas as {falhas} transcrição(ões) da janela falharam")
        return []
    cortes = curador.curar(fonte.live, fonte.live.get("titulo", ""), com_texto)
    escolhidos = []
    for corte in cortes or []:
        idx = corte.get("id")
        if not isinstance(idx, int) or not 0 <= idx < len(com_texto):
            continue
        base = com_texto[idx]
        escolhidos.append({**corte, "trans_ini": base["trans_ini"], "palavras": base["palavras"]})
    log(f"{fonte.nome_janela(janela)}: {len(cands)} candidato(s) → {len(escolhidos)} escolhido(s)", ORIGEM_LOG)
    return escolhidos


def _processar_com_tentativas(fonte, janela, estado, espera_base=ESPERA_TEMPORARIO_BASE_SEG):
    """Roda a janela tentando de novo em erro temporário/inesperado e marca como feita no fim."""
    while True:
        try:
            escolhidos = processar_janela(fonte, janela)
        except ErroTemporario as e:
            n = estado.falha_janela(janela["id"], "tentativas_temporarias")
            log(f"{fonte.nome_janela(janela)}: erro temporário: {e} (tentativa {n}/{JANELA_MAX_TENTATIVAS_TEMPORARIO})",
                ORIGEM_LOG)
            if n < JANELA_MAX_TENTATIVAS_TEMPORARIO:
                time.sleep(espera_base * n)
                continue
            estado.marcar_janela(janela["id"], erro=f"temporário: {e}"[:300])
            return []
        except Exception as e:  # noqa: BLE001 - uma janela ruim não pode parar o vídeo inteiro
            n = estado.falha_janela(janela["id"])
            log(f"{fonte.nome_janela(janela)}: erro {type(e).__name__}: {e} (tentativa {n})", ORIGEM_LOG)
            if n < JANELA_MAX_TENTATIVAS:
                continue
            estado.marcar_janela(janela["id"], erro=f"{type(e).__name__}: {e}"[:300])
            return []
        estado.marcar_janela(janela["id"], escolhidos)
        return escolhidos


# =====================================================================================  render

def _renderizar(fonte, escolhido, pasta_saida):
    """Gera o corte na pasta de saída. Devolve o registro (ou None se o render falhou)."""
    segs = fonte.segmentos()
    ini, fim = float(escolhido["ini"]), float(escolhido["fim"])
    try:
        segs_layout = trechos.blocos_cobrindo(segs, ini, fim) or segs
        lay = _layout_seguro(segs_layout, ini, fim)
        saidas = render.renderizar(segs, escolhido, lay, escolhido.get("palavras") or [], fonte.live, pasta_saida,
                                   origem_palavras=escolhido.get("trans_ini"))
    except Exception as e:  # noqa: BLE001 - um render ruim não derruba os outros
        log(f"render de {mmss(ini)} falhou: {type(e).__name__}: {e}", ORIGEM_LOG)
        return None
    return {
        "titulo": escolhido.get("titulo", ""),
        "nota": float(escolhido.get("nota") or NOTA_PADRAO),
        "origem": escolhido.get("origem", ""),
        "ini": round(ini, CASAS_TEMPO), "fim": round(fim, CASAS_TEMPO),
        "pico": round(float(escolhido.get("pico", ini)), CASAS_TEMPO),
        "duracao": round(fim - ini, CASAS_TEMPO),
        "layout": (lay or {}).get("tipo", "jogo"),
        "criado": time.time(),
        **{k: str(v) for k, v in saidas.items()},
    }


# =====================================================================================  entrada principal

def cortar(caminho, saida=None, max_cortes=None, canal=None, plataforma="", refazer=False, trabalhadores=None,
           estado=None) -> list:
    """Processa um vídeo (ou pasta de blocos .ts) inteiro e devolve os registros dos cortes NOVOS.

    `saida`: pasta dos cortes (padrão: `saida` ao lado do vídeo). `max_cortes`: total por vídeo, contando os já
    feitos (padrão MAX_CORTES do .env; 0 = sem limite). `canal`: nome do criador para os créditos.
    """
    fonte = fonte_de_caminho(caminho, canal=canal, plataforma=plataforma)
    if not (canal or "").strip():
        log(f"créditos vão para \"{fonte.live['canal']}\" (nome do arquivo); use --canal para dar o nome certo do "
            "criador", ORIGEM_LOG)
    # absoluto: o ffmpeg do render roda com outra pasta atual (a temporária do corte)
    pasta_saida = (Path(saida) if saida else pasta_saida_padrao(caminho)).resolve()
    pasta_saida.mkdir(parents=True, exist_ok=True)
    limite = config.MAX_CORTES if max_cortes is None else max(0, int(max_cortes))
    estado = estado or Estado(config.ESTADO / f"{fonte.chave}.json")
    if refazer:
        estado.esquecer()

    janelas = [j for j in planejar_janelas(fonte.blocos()) if not estado.janela_feita(j["id"])]
    if janelas:
        n = max(1, trabalhadores or config.TRABALHADORES)
        log(f"{fonte.caminho.name}: {mmss(fonte.duracao)} de vídeo, {len(janelas)} janela(s) para analisar",
            ORIGEM_LOG)
        fonte.energia(0.0, 0.0)              # lê o áudio inteiro uma vez antes de abrir as threads
        with ThreadPoolExecutor(max_workers=n) as pool:
            tarefas = pool.map(lambda j: _processar_com_tentativas(fonte, j, estado), janelas)
            for i, _ in enumerate(tarefas, 1):
                log(f"análise {i}/{len(janelas)} pronta", ORIGEM_LOG)

    selecionados = selecionar_cortes(estado.escolhidos(), estado.cortes(), limite)
    if not selecionados:
        feitos = len(estado.cortes())
        log("nenhum corte novo" + (f" ({feitos} já feito(s); use --refazer para recomeçar)" if feitos else ""),
            ORIGEM_LOG)
        return []
    log(f"gerando {len(selecionados)} corte(s) em {pasta_saida}", ORIGEM_LOG)
    novos = []
    for i, escolhido in enumerate(selecionados, 1):
        log(f"corte {i}/{len(selecionados)}: {mmss(escolhido['ini'])} nota {escolhido.get('nota', 0)} — "
            f"{escolhido.get('titulo', '')}", ORIGEM_LOG)
        registro = _renderizar(fonte, escolhido, pasta_saida)
        if registro:
            estado.registrar_corte(registro)
            novos.append(registro)
    return novos


# =====================================================================================  diagnóstico

def _checar_comando(nome, cmd):
    try:
        saida = util.rodar(cmd, timeout=60)
        primeira = (saida.strip().splitlines() or [""])[0][:90]
        print(f"OK     {nome}: {primeira}", flush=True)
        return True
    except Exception as e:  # noqa: BLE001 - qualquer erro aqui só vira linha do diagnóstico
        print(f"FALTA  {nome}: {str(e).splitlines()[0][:150] if str(e) else type(e).__name__}", flush=True)
        return False


def testar(ia=False) -> bool:
    """Diagnóstico: ffmpeg, bibliotecas, modelo de rosto, fontes e IA. True se o essencial está ok."""
    import importlib

    from cortalive import llm

    ok = True
    ok &= _checar_comando("ffmpeg", [config.FFMPEG, "-version"])
    ok &= _checar_comando("ffprobe", [config.FFPROBE, "-version"])
    for modulo, pip in (("faster_whisper", "faster-whisper"), ("cv2", "opencv-python"), ("numpy", "numpy"),
                        ("requests", "requests"), ("dotenv", "python-dotenv")):
        try:
            importlib.import_module(modulo)
            print(f"OK     python: {pip}", flush=True)
        except ImportError as e:
            ok = False
            print(f"FALTA  python: {pip} ({e}) → pip install -r requirements.txt", flush=True)
    print(f"INFO   whisper: modelo {config.WHISPER_MODELO}, idioma {config.WHISPER_IDIOMA} "
          "(baixa sozinho na 1ª vez)", flush=True)
    for nome, caminho in (("modelo de rosto (YuNet)", config.MODELO_ROSTO), ("fontes", config.FONTES)):
        existe = Path(caminho).exists() and (Path(caminho).is_file() or any(Path(caminho).iterdir()))
        ok &= existe
        print(f"{'OK    ' if existe else 'FALTA '} {nome}: {caminho}", flush=True)
    provs = [n for n, _ in llm.provedores()]
    if not provs:
        print("AVISO  IA: nenhuma chave configurada — os cortes saem escolhidos pelo volume, sem títulos da IA",
              flush=True)
    elif ia:
        print("INFO   IA: testando cada provedor de verdade…", flush=True)
        if not llm.testar_todos():
            print("AVISO  IA: nenhum provedor respondeu — os cortes saem escolhidos pelo volume", flush=True)
    else:
        print(f"OK     IA configurada: {', '.join(provs)} (teste real: python -m cortalive testar --ia)", flush=True)
    print("TUDO PRONTO" if ok else "FALTAM ITENS (veja as linhas FALTA)", flush=True)
    return ok

