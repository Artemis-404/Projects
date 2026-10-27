"""Lab certificate generation (FR-03, NFR-07, SDD 6.1).

Mutual TLS needs a private CA plus a client and a server certificate.  UQAB
never invents cryptography: the certificates are ordinary X.509 objects made by
the system ``openssl`` tool, and the transport uses Python's ``ssl`` module with
TLS 1.3 only.  Certificates are lab material and are never committed to Git.

On Windows, where ``openssl`` is often absent, the build ships the lab
certificates generated here; ``python -m tunnel.certs --check`` verifies them.
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from config import Config, load_config

CA_DAYS = 3650
LEAF_DAYS = 3650


class CertError(RuntimeError):
    """Certificate material is missing or cannot be generated."""


@dataclass(frozen=True, slots=True)
class CertPaths:
    ca: Path
    cert: Path
    key: Path
    server_cert: Path
    server_key: Path

    @classmethod
    def from_config(cls, cfg: Config) -> CertPaths:
        return cls(
            ca=cfg.path("tls.ca"),
            cert=cfg.path("tls.cert"),
            key=cfg.path("tls.key"),
            server_cert=cfg.path("tls.server_cert"),
            server_key=cfg.path("tls.server_key"),
        )

    def all_present(self) -> bool:
        return all(p.is_file() and p.stat().st_size > 0 for p in self.iter_files())

    def iter_files(self) -> tuple[Path, ...]:
        return (self.ca, self.cert, self.key, self.server_cert, self.server_key)


def _openssl() -> str:
    binary = shutil.which("openssl")
    if not binary:
        raise CertError(
            "openssl not found: install it or ship pre-generated lab certificates "
            "(python -m tunnel.certs --check explains what is missing)"
        )
    return binary


def _run(args: list[str]) -> None:
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise CertError(f"openssl failed ({' '.join(args[:3])}...): {result.stderr.strip()}")


def generate(paths: CertPaths, force: bool = False) -> list[Path]:
    """Create a CA, a server certificate and a client certificate."""
    if paths.all_present() and not force:
        return list(paths.iter_files())

    paths.ca.parent.mkdir(parents=True, exist_ok=True)
    openssl = _openssl()
    common = "/O=Meta Core Solution/OU=UQAB Lab"
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        ca_key = paths.ca.with_suffix(".key")
        _run(
            [
                openssl, "req", "-x509", "-newkey", "rsa:3072", "-sha256", "-nodes",
                "-keyout", str(ca_key), "-out", str(paths.ca), "-days", str(CA_DAYS),
                "-subj", f"/CN=UQAB Lab CA{common}",
                "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
                "-addext", "keyUsage=critical,keyCertSign,cRLSign",
            ]
        )

        def leaf(name: str, key_path: Path, cert_path: Path, subject: str, eku: str,
                 san: str) -> None:
            csr = tmpdir / f"{name}.csr"
            ext = tmpdir / f"{name}.ext"
            ext.write_text(
                "basicConstraints=critical,CA:FALSE\n"
                "keyUsage=critical,digitalSignature,keyEncipherment\n"
                f"extendedKeyUsage={eku}\n"
                f"subjectAltName={san}\n",
                encoding="utf-8",
            )
            _run(
                [
                    openssl, "req", "-newkey", "rsa:2048", "-sha256", "-nodes",
                    "-keyout", str(key_path), "-out", str(csr), "-subj", subject,
                ]
            )
            _run(
                [
                    openssl, "x509", "-req", "-in", str(csr), "-CA", str(paths.ca),
                    "-CAkey", str(ca_key), "-CAcreateserial", "-out", str(cert_path),
                    "-days", str(LEAF_DAYS), "-sha256", "-extfile", str(ext),
                ]
            )
            # Windows has no POSIX modes; the file is protected by the install dir.
            with contextlib.suppress(OSError):
                key_path.chmod(0o600)

        leaf(
            "server", paths.server_key, paths.server_cert,
            f"/CN=uqab-bridge{common}", "serverAuth",
            "DNS:uqab-bridge,DNS:localhost,IP:127.0.0.1",
        )
        leaf(
            "client", paths.key, paths.cert,
            f"/CN=uqab-client{common}", "clientAuth",
            "DNS:uqab-client",
        )

    missing = [str(p) for p in paths.iter_files() if not p.is_file()]
    if missing:  # pragma: no cover - defensive
        raise CertError(f"certificate generation incomplete: {missing}")
    return list(paths.iter_files())


def ensure(paths: CertPaths) -> CertPaths:
    """Generate certificates when missing, otherwise reuse them."""
    if not paths.all_present():
        generate(paths)
    return paths


def check(paths: CertPaths) -> list[str]:
    """Return human-readable problems with the certificate material."""
    problems: list[str] = []
    for path in paths.iter_files():
        if not path.is_file():
            problems.append(f"missing {path}")
        elif path.stat().st_size == 0:
            problems.append(f"empty {path}")
    if not problems:
        try:
            result = subprocess.run(
                [_openssl(), "x509", "-in", str(paths.server_cert), "-noout", "-subject", "-dates"],
                capture_output=True, text=True, check=False,
            )
        except CertError as exc:
            problems.append(str(exc))
        else:
            if result.returncode != 0:
                problems.append(f"server certificate unreadable: {result.stderr.strip()}")
    return problems


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    cfg = load_config()
    paths = CertPaths.from_config(cfg)
    # ``--out DIR`` writes the certificates somewhere else (used by the packager,
    # which pre-generates them inside the bundle).
    if "--out" in args:
        index = args.index("--out")
        if index + 1 >= len(args):
            print("--out needs a directory")
            return 2
        directory = Path(args[index + 1]).expanduser()
        directory.mkdir(parents=True, exist_ok=True)
        paths = CertPaths(
            ca=directory / "ca.crt",
            cert=directory / "client.crt",
            key=directory / "client.key",
            server_cert=directory / "server.crt",
            server_key=directory / "server.key",
        )
    force = "--force" in args
    if "--check" in args:
        problems = check(paths)
        if problems:
            for problem in problems:
                print(f"MISSING: {problem}")
            return 1
        print("certificates present and readable")
        return 0
    generate(paths, force=force)
    print(f"wrote {len(paths.iter_files())} certificate file(s) under {paths.ca.parent}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
