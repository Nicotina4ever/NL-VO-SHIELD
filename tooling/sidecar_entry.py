"""NL VO Shield - PyInstaller entry point for the frozen engine.

The frozen executable accepts the same CLI arguments as
`python -m nl_vo_shield.cli`, and `--rpc` runs the JSON-RPC stdio sidecar
(the same protocol as `python -m nl_vo_shield.rpc --data-dir <dir>`).
"""
import sys


def _call(module, args):
    fn = getattr(module, "main", None)
    if fn is None:
        raise SystemExit(f"{module.__name__}.main() is required by the frozen sidecar")
    try:
        argc = fn.__code__.co_argcount
    except AttributeError:
        argc = 0
    if argc >= 1:
        return fn(args)
    return fn()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--rpc":
        from nl_vo_shield import rpc
        return _call(rpc, argv[1:])
    from nl_vo_shield import cli
    return _call(cli, argv)


if __name__ == "__main__":
    result = main()
    raise SystemExit(result if isinstance(result, int) else 0)
