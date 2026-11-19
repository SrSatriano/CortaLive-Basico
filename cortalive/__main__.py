"""Linha de comando do CortaLive Básico.

  python -m cortalive cortar <vídeo|pasta> [--saida PASTA] [--max-cortes N] [--canal NOME]
  python -m cortalive testar [--ia]      diagnóstico: ffmpeg, bibliotecas, fontes e IA
"""
import argparse
import sys
from pathlib import Path

from cortalive import __version__, config
from cortalive.util import definir_arquivo_log, mmss


def _argumentos(argv):
    p = argparse.ArgumentParser(
        prog="python -m cortalive",
        description=f"CortaLive Básico {__version__}: transforma um vídeo ou live gravada em cortes verticais "
                    "1080x1920 com legenda.")
    sub = p.add_subparsers(dest="comando")

    c = sub.add_parser("cortar", help="corta um vídeo (MP4/MKV/TS) ou uma pasta com blocos .ts")
    c.add_argument("caminho", help="arquivo de vídeo ou pasta com os blocos .ts da live")
    c.add_argument("--saida", help="pasta dos cortes (padrão: 'saida' ao lado do vídeo)")
    c.add_argument("--max-cortes", type=int, default=None,
                   help=f"quantos cortes no máximo (padrão {config.MAX_CORTES}; 0 = sem limite)")
    c.add_argument("--canal", help="nome do criador do vídeo original, para os créditos (padrão: nome do arquivo)")
    c.add_argument("--plataforma", default="", help="onde o vídeo original está (ex.: Twitch, YouTube, Kick)")
    c.add_argument("--refazer", action="store_true", help="esquece o que já foi feito nesse vídeo e recomeça")
    c.add_argument("--trabalhadores", type=int, default=None,
                   help=f"partes analisadas ao mesmo tempo (padrão {config.TRABALHADORES})")

    t = sub.add_parser("testar", help="confere se está tudo instalado")
    t.add_argument("--ia", action="store_true", help="faz uma chamada real em cada IA configurada")
    return p, p.parse_args(argv)


def _cortar(args):
    from cortalive import cortador
    try:
        cortes = cortador.cortar(args.caminho, saida=args.saida, max_cortes=args.max_cortes, canal=args.canal,
                                 plataforma=args.plataforma, refazer=args.refazer, trabalhadores=args.trabalhadores)
    except (FileNotFoundError, ValueError, RuntimeError) as e:
        print(f"Erro: {e}", file=sys.stderr)
        return 1
    print()
    for c in cortes:
        print(f"{c['nota']:>4.1f}  {mmss(c['ini'])}–{mmss(c['fim'])}  {c['titulo']}\n      {c['mp4']}")
    destino = Path(args.saida or cortador.pasta_saida_padrao(args.caminho)).resolve()
    print(f"\n{len(cortes)} corte(s) novo(s) em {destino}")
    return 0


def main(argv=None):
    parser, args = _argumentos(sys.argv[1:] if argv is None else argv)
    if args.comando == "cortar":
        definir_arquivo_log(config.ESTADO / "cortalive.log")
        return _cortar(args)
    if args.comando == "testar":
        from cortalive import cortador
        return 0 if cortador.testar(ia=args.ia) else 1
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
