# coding: utf-8
"""Command dispatcher for the modular MiniQMT strategy package."""

import sys


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("backtest", "bt"):
        from . import backtest

        return backtest.main(argv[1:])
    from . import core

    return core.main(argv)
