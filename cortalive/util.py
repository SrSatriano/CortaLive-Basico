"""Utilitários comuns: log, subprocessos, ffprobe, nomes de arquivo e JSON atômico."""
import json
import os
import re
import subprocess
import sys
import threading
import time
import unicodedata
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - console sem suporte; segue com o padrão
        pass

_LOCK_LOG = threading.Lock()
_ARQ_LOG = None


def definir_arquivo_log(caminho: Path):
    """Passa a gravar o log também em arquivo (além do console)."""
    global _ARQ_LOG
    caminho.parent.mkdir(parents=True, exist_ok=True)
    _ARQ_LOG = caminho


def log(msg, origem="cortalive"):
    linha = f"[{time.strftime('%H:%M:%S')}] [{origem}] {msg}"
    with _LOCK_LOG:
        print(linha, flush=True)
        if _ARQ_LOG:
            try:
                with open(_ARQ_LOG, "a", encoding="utf-8") as f:
                    f.write(linha + "\n")
            except OSError:
                pass


def rodar(cmd, cwd=None, timeout=None):
    """Roda um comando e devolve o stdout; levanta RuntimeError com o fim do stderr se falhar."""
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=cwd, timeout=timeout,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0:
        raise RuntimeError(f"Falhou ({r.returncode}): {' '.join(map(str, cmd))[:300]}\n{r.stderr[-1500:]}")
    return r.stdout


def probe(caminho):
    """Info básica de um vídeo: largura, altura, fps, duração (s) e se tem áudio."""
    from cortalive.config import FFPROBE
    saida = rodar([FFPROBE, "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(caminho)])
    j = json.loads(saida)
    video = next((s for s in j["streams"] if s["codec_type"] == "video"), None)
    if video is None:
        raise RuntimeError(f"sem trilha de vídeo: {caminho}")
    num, den = video.get("avg_frame_rate", video.get("r_frame_rate", "30/1")).split("/")
    fps = float(num) / float(den) if float(den) else 30.0
    dur = float(j.get("format", {}).get("duration") or video.get("duration") or 0)
    return {"w": int(video["width"]), "h": int(video["height"]), "fps": fps, "dur": dur,
            "audio": any(s["codec_type"] == "audio" for s in j["streams"])}


def slug(txt, n=40):
    t = unicodedata.normalize("NFKD", str(txt)).encode("ascii", "ignore").decode()
    t = re.sub(r"[^A-Za-z0-9]+", "_", t).strip("_")
    return t[:n] or "x"


def mmss(seg):
    seg = max(0, int(seg))
    h, r = divmod(seg, 3600)
    return f"{h}:{r // 60:02d}:{r % 60:02d}" if h else f"{r // 60:02d}:{r % 60:02d}"


def ler_json(caminho: Path, padrao=None):
    try:
        return json.loads(Path(caminho).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return padrao


# No Windows, os.replace falha com PermissionError enquanto outro processo (antivírus, indexador) está com o
# destino aberto para leitura. Tenta de novo com espera crescente.
TENTATIVAS_SUBSTITUIR = 8
ESPERA_SUBSTITUIR_SEG = 0.05          # espera da n-ésima nova tentativa = n × isto (total ~1,8 s)


def salvar_json(caminho: Path, dados):
    """Escrita atômica (tmp + replace): nunca deixa arquivo pela metade.

    Colisão com um leitor (PermissionError no Windows) → tenta de novo algumas vezes; esgotou → levanta.
    """
    caminho = Path(caminho)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    # tmp único por thread/processo: duas escritas simultâneas do mesmo arquivo não pisam no mesmo tmp
    tmp = caminho.with_name(f"{caminho.name}.{os.getpid()}_{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(dados, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        for tentativa in range(TENTATIVAS_SUBSTITUIR):
            try:
                os.replace(tmp, caminho)
                return
            except PermissionError:
                if tentativa == TENTATIVAS_SUBSTITUIR - 1:
                    raise
                time.sleep(ESPERA_SUBSTITUIR_SEG * (tentativa + 1))
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError as e:
            log(f"não consegui apagar o temporário {tmp.name}: {e}", "util")

