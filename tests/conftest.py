"""Shared fixtures for the UQAB test suite."""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, TypeVar

import pytest

from config import Config, load_config
from persona.validator import Limits
from tunnel.certs import CertPaths, generate
from tunnel.tls import TLSSettings

T = TypeVar("T")


def run(coro: Coroutine[Any, Any, T], timeout: float = 60.0) -> T:
    """Run one coroutine in a fresh event loop (no pytest-asyncio dependency)."""

    async def wrapper() -> T:
        return await asyncio.wait_for(coro, timeout=timeout)

    return asyncio.run(wrapper())


@pytest.fixture(scope="session")
def cert_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("uqab-certs")
    if not shutil.which("openssl"):
        pytest.skip("openssl is required to generate the lab certificates")
    paths = CertPaths(
        ca=directory / "ca.crt",
        cert=directory / "client.crt",
        key=directory / "client.key",
        server_cert=directory / "server.crt",
        server_key=directory / "server.key",
    )
    generate(paths)
    return directory


@pytest.fixture(scope="session")
def tls_settings(cert_dir: Path) -> TLSSettings:
    return TLSSettings(
        ca=str(cert_dir / "ca.crt"),
        cert=str(cert_dir / "client.crt"),
        key=str(cert_dir / "client.key"),
        server_cert=str(cert_dir / "server.crt"),
        server_key=str(cert_dir / "server.key"),
        hostname="uqab-bridge",
    )


@pytest.fixture()
def cfg(tmp_path: Path, cert_dir: Path) -> Config:
    """Repository configuration with every writable path redirected to tmp."""
    base = load_config()
    return base.with_overrides(
        **{
            "paths.run": str(tmp_path / "run"),
            "paths.data": str(tmp_path / "data"),
            "paths.sqlite": str(tmp_path / "data" / "uqab.sqlite"),
            "profile.path": str(tmp_path / "data" / "profile" / "profile.json"),
            "profile.opt_in_flag": str(tmp_path / "data" / "profile" / "opt_in.json"),
            "tls.ca": str(cert_dir / "ca.crt"),
            "tls.cert": str(cert_dir / "client.crt"),
            "tls.key": str(cert_dir / "client.key"),
            "tls.server_cert": str(cert_dir / "server.crt"),
            "tls.server_key": str(cert_dir / "server.key"),
            "persona.profiles_dir": str(base.path("persona.profiles_dir")),
            "cover.retry_s": 0.2,
            "cover.stats_interval_s": 0.2,
            "pt.connect_timeout_s": 5,
        }
    )


@pytest.fixture(scope="session")
def limits() -> Limits:
    return Limits.from_config(load_config())
