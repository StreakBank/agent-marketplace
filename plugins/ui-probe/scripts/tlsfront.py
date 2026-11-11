#!/usr/bin/env python3
"""tlsfront - TLS in front of a plain local backend, for an app on the iOS Simulator.

An iOS app refuses plain HTTP (App Transport Security), so it cannot reach a local backend that
speaks HTTP unless its own security settings are loosened. This listens with TLS on
127.0.0.1:<port> and pipes the bytes both ways to 127.0.0.1:<to-port>, with a certificate for
`localhost` signed by a throwaway certificate authority kept in a state folder. Add that CA to the
simulator (`xcrun simctl keychain <udid> add-root-cert <state>/ca.pem`) and the app reaches the
backend at https://localhost:<port> with no change to the app. The CA is a throwaway for a simulator:
never add it to a phone's or a Mac's own trust store. This tool never touches a trust store, and it
shapes nothing (the proxy does that).

The CA and the certificate are made with the `openssl` on PATH on first use and reused while valid:
ca.pem (CA:TRUE with pathlen:0, critical; name constraints permitting DNS:localhost only, critical; key
usage certificate signing; RSA 2048; SHA-256; 365 days; its key is deleted once the certificate is signed),
leaf.pem and leaf.key for localhost (subject alternative name DNS:localhost; extended key usage
server authentication; not a CA; 365 days). Both are made again when either has expired or is within
7 days of it, and the output says so: the simulator then needs the new CA. A folder it makes is 0700
and the key 0600; a folder that is not empty and holds no marker of this tool, or a folder or key that
others can read, is refused, never quietly changed.

With --log, the front also reads the requests it pipes and writes one JSON line per request (its
method and its path without the query string), per connection closed or refused, and when it starts
listening (see httppipe.py, which this front and `ui-probe http-front` share). It offers only
http/1.1 in ALPN, which is what keeps the requests readable. No header value, query string or body
is logged, and no byte is changed.

usage: ui-probe tls-front --port 18443 --to-port 8080 --state <dir> [--log <file>]
       ui-probe tls-front --state <dir> --ca           (print the path of ca.pem, making it if absent)
"""
from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import ssl
import stat
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from httppipe import Front as PipeFront, FrontError, port  # noqa: E402  (re-exported: FrontError, port)

RENEW_WITHIN_S = 7 * 86400
DAYS = 365
FILES = ("ca.pem", "leaf.pem", "leaf.key")
MARKER = ".ui-probe-tls-front"     # written into a folder this tool made or adopted while it was empty
CA_CNF = """[req]
distinguished_name = dn
x509_extensions = v3_ca
prompt = no
[dn]
CN = ui-probe local backend CA (throwaway, for a simulator only)
[v3_ca]
basicConstraints = critical, CA:TRUE, pathlen:0
keyUsage = critical, keyCertSign, cRLSign
nameConstraints = critical, permitted;DNS:localhost
subjectKeyIdentifier = hash
"""
LEAF_EXT = """subjectAltName = DNS:localhost
extendedKeyUsage = serverAuth
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
"""


def _openssl(state: Path, *args: str) -> None:
    exe = shutil.which("openssl")
    if exe is None:
        raise FrontError("openssl is not on PATH; it makes the throwaway certificate authority")
    proc = subprocess.run([exe, *args], cwd=state, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise FrontError(f"openssl {args[0]} failed (exit {proc.returncode}): {(proc.stderr or proc.stdout).strip()[-400:]}")


def _expires_soon(state: Path, name: str) -> bool:
    exe = shutil.which("openssl")
    if exe is None:
        raise FrontError("openssl is not on PATH; it checks the certificate's dates")
    proc = subprocess.run([exe, "x509", "-checkend", str(RENEW_WITHIN_S), "-noout", "-in", str(state / name)],
                          capture_output=True, text=True, timeout=60)
    return proc.returncode != 0


def _readable_by_others(path: Path) -> bool:
    return bool(path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO))


def ensure_state(state: Path) -> tuple[Path, str | None]:
    """The state folder with a valid CA and certificate, made or remade as needed. Returns the folder
    and a message when the files were made (None when reused). A folder that is not empty and does not
    hold this tool's marker is refused; the mode of, and the files in, a folder this tool did not make
    are never changed."""
    if state.exists() and not state.is_dir():
        raise FrontError(f"{state} is not a folder")
    marker = state / MARKER
    if state.is_dir() and not marker.exists():
        if any(state.iterdir()):
            raise FrontError(f"{state} is not empty and was not made by tls-front (it holds no {MARKER}); refused: "
                             "give an empty or a new folder")
        if _readable_by_others(state):
            raise FrontError(f"{state} was not made by tls-front and others can open it (mode "
                             f"{stat.S_IMODE(state.stat().st_mode):o}); refused, not changed: give a new folder, or run "
                             f"`chmod 700 {state}` if that is what you want")
        marker.write_text("made by ui-probe tls-front\n")       # an empty folder of the user's, adopted as it is
    elif not state.exists():
        state.mkdir(mode=0o700, parents=True)
        os.chmod(state, 0o700)
        marker.write_text("made by ui-probe tls-front\n")
    if _readable_by_others(state):
        raise FrontError(f"{state} holds keys and others can open it (mode {stat.S_IMODE(state.stat().st_mode):o}); "
                         f"refused, not changed: run `chmod 700 {state}` if that is what you want")
    key = state / "leaf.key"
    if key.exists() and _readable_by_others(key):
        raise FrontError(f"{key} can be read by others (mode {stat.S_IMODE(key.stat().st_mode):o}); refused, not "
                         f"changed: run `chmod 600 {key}` if that is what you want")
    reason = None
    if not all((state / f).exists() for f in FILES):
        reason = "none was there"
    elif _expires_soon(state, "ca.pem") or _expires_soon(state, "leaf.pem"):
        reason = f"the old one had expired or would within {RENEW_WITHIN_S // 86400} days"
    if reason is None:
        return state, None
    for f in FILES + ("ca.key", "leaf.csr", "ca.srl"):
        (state / f).unlink(missing_ok=True)
    (state / "ca.cnf").write_text(CA_CNF)
    (state / "leaf.ext").write_text(LEAF_EXT)
    old = os.umask(0o077)           # the keys are written 0600 from the start
    try:
        _openssl(state, "req", "-x509", "-new", "-newkey", "rsa:2048", "-nodes", "-sha256", "-days", str(DAYS),
                 "-keyout", "ca.key", "-out", "ca.pem", "-config", "ca.cnf")
        _openssl(state, "req", "-new", "-newkey", "rsa:2048", "-nodes", "-sha256", "-keyout", "leaf.key",
                 "-out", "leaf.csr", "-subj", "/CN=localhost")
        _openssl(state, "x509", "-req", "-in", "leaf.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial",
                 "-out", "leaf.pem", "-days", str(DAYS), "-sha256", "-extfile", "leaf.ext")
    finally:
        os.umask(old)
        # The CA's key is not kept: a renewal makes a new CA anyway, and a key that does not exist cannot sign.
        (state / "ca.key").unlink(missing_ok=True)
    os.chmod(key, 0o600)
    return state, (f"made a new throwaway certificate authority and a certificate for localhost in {state} ({reason}); "
                   f"a simulator needs the new CA: xcrun simctl keychain <udid> add-root-cert {state / 'ca.pem'}")


def server_context(state: Path) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(str(state / "leaf.pem"), str(state / "leaf.key"))
    ctx.set_alpn_protocols(["http/1.1"])
    return ctx


class Front(PipeFront):
    """TLS on 127.0.0.1:<port> with the state folder's certificate, piped to 127.0.0.1:<to_port>."""

    def __init__(self, port: int, to_port: int, state: Path, log_path: str | None):
        super().__init__(port, to_port, log_path)
        self.state = state

    def ssl_context(self) -> ssl.SSLContext:
        return server_context(self.state)

    def listening_line(self, port: int) -> str:
        return f"listening on https://localhost:{port} -> http://127.0.0.1:{self.to_port}"


def parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="ui-probe tls-front", description=__doc__.split("\n\n")[0])
    ap.add_argument("--state", required=True, help="the folder that keeps the throwaway CA and the certificate")
    ap.add_argument("--ca", action="store_true", help="print the path of ca.pem (making it if absent) and exit")
    ap.add_argument("--port", type=port, help="the TLS port to listen on, on 127.0.0.1")
    ap.add_argument("--to-port", type=port, help="the plain backend's port on 127.0.0.1")
    ap.add_argument("--log", default=None, help="one JSON line per request read, per connection closed or refused, "
                                                "and when the front starts listening")
    args = ap.parse_args(argv)
    if not args.ca:
        if args.port is None or args.to_port is None:
            ap.error("--port and --to-port are required (or --ca)")
        if args.port == args.to_port:
            ap.error("--port and --to-port must differ")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        state, made = ensure_state(Path(args.state))
    except FrontError as exc:
        print(f"ui-probe tls-front: {exc}", file=sys.stderr)
        return 2
    if made:
        print(made, file=sys.stderr, flush=True)
    if args.ca:
        print(state / "ca.pem")
        return 0
    try:
        asyncio.run(Front(args.port, args.to_port, state, args.log).serve())
    except FrontError as exc:
        print(f"ui-probe tls-front: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"ui-probe tls-front: cannot listen on 127.0.0.1:{args.port}: {exc.strerror or exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
