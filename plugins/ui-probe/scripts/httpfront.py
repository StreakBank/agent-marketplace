#!/usr/bin/env python3
"""httpfront - a plain front for a plain local backend, for an app on an Android emulator.

Listens on 127.0.0.1:<port> and pipes the bytes both ways to 127.0.0.1:<to-port>, like
`ui-probe tls-front` without TLS and without a state folder. An Android emulator reaches this
machine's loopback address as 10.0.2.2, so an app pointed at http://10.0.2.2:<port> goes through
this front. Its point is the log: with --log it reads the requests it pipes and writes one JSON
line per request (its method and its path without the query string), per connection closed or
refused, and when it starts listening (see httppipe.py). `ui-probe run --requests <log>` counts
them per step. No header value, query string or body is logged, and no byte is changed; it shapes
nothing (the proxy does that).

usage: ui-probe http-front --port 18080 --to-port 8080 [--log <file>]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from httppipe import Front, FrontError, port  # noqa: E402


def parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="ui-probe http-front", description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=port, required=True, help="the port to listen on, on 127.0.0.1")
    ap.add_argument("--to-port", type=port, required=True, help="the plain backend's port on 127.0.0.1")
    ap.add_argument("--log", default=None, help="one JSON line per request read, per connection closed or refused, "
                                                "and when the front starts listening")
    args = ap.parse_args(argv)
    if args.port == args.to_port:
        ap.error("--port and --to-port must differ")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        asyncio.run(Front(args.port, args.to_port, args.log).serve())
    except FrontError as exc:
        print(f"ui-probe http-front: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"ui-probe http-front: cannot listen on 127.0.0.1:{args.port}: {exc.strerror or exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
