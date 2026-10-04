"""Process-local TLS root configuration for T-Invest gRPC clients."""

from __future__ import annotations

import os
import warnings
from pathlib import Path


def _resolve_path(value: str, *, base_dir: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def _split_paths(raw: str, *, base_dir: Path) -> list[Path]:
    out: list[Path] = []
    for part in raw.replace(";", ",").split(","):
        value = part.strip().strip('"')
        if value:
            out.append(_resolve_path(value, base_dir=base_dir))
    return out


def _certifi_bundle_path() -> Path:
    import certifi

    return Path(certifi.where()).resolve()


def configure_tinvest_grpc_ssl_roots(
    *,
    extra_root_certs_raw: str,
    bundle_path_raw: str,
    base_dir: Path,
) -> Path | None:
    """Build a gRPC roots bundle for this process without touching OS trust store.

    ``GRPC_DEFAULT_SSL_ROOTS_FILE_PATH`` replaces gRPC's default roots, so the
    generated file must contain both certifi's public roots and the extra roots.
    """
    extra_paths = _split_paths(extra_root_certs_raw, base_dir=base_dir)
    if not extra_paths:
        return None

    missing = [str(path) for path in extra_paths if not path.is_file()]
    if missing:
        raise ValueError(
            "TINVEST_GRPC_EXTRA_ROOT_CERTS содержит несуществующие файлы: "
            + ", ".join(missing)
        )

    bundle_path = (
        _resolve_path(bundle_path_raw.strip(), base_dir=base_dir)
        if bundle_path_raw.strip()
        else (base_dir / "data" / "certs" / "tinvest_grpc_roots_bundle.pem").resolve()
    )
    bundle_path.parent.mkdir(parents=True, exist_ok=True)

    chunks: list[bytes] = []
    chunks.append(_certifi_bundle_path().read_bytes())
    for path in extra_paths:
        chunks.append(path.read_bytes())

    with bundle_path.open("wb") as fh:
        for chunk in chunks:
            if chunk and not chunk.endswith(b"\n"):
                chunk += b"\n"
            fh.write(chunk)
            fh.write(b"\n")

    os.environ["GRPC_DEFAULT_SSL_ROOTS_FILE_PATH"] = str(bundle_path)
    return bundle_path


def try_configure_tinvest_grpc_ssl_roots(
    *,
    extra_root_certs_raw: str,
    bundle_path_raw: str,
    base_dir: Path,
) -> Path | None:
    """Best-effort early startup wrapper that does not break config imports."""
    try:
        return configure_tinvest_grpc_ssl_roots(
            extra_root_certs_raw=extra_root_certs_raw,
            bundle_path_raw=bundle_path_raw,
            base_dir=base_dir,
        )
    except Exception as exc:  # noqa: BLE001
        warnings.warn(
            f"T-Invest gRPC extra certificates were not configured: {type(exc).__name__}: {exc}",
            RuntimeWarning,
            stacklevel=2,
        )
        return None
