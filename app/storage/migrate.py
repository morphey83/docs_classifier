"""Copy every blob from one backend to another.

Used once when switching ``STORAGE_BLOBS`` (local -> s3, or back). Idempotent:
keys already present at the destination are skipped, so it is safe to re-run
after an interruption.

    # dry run — just count what would move
    python -m app.storage.migrate --to s3

    # actually copy
    python -m app.storage.migrate --to s3 --commit

    # copy, then remove each source blob once the copy is verified
    python -m app.storage.migrate --to s3 --commit --delete-source

``--to`` names the *destination*; the source is the other backend. Flip the
config (``STORAGE_BLOBS``) only after a successful ``--commit`` run.
"""

from __future__ import annotations

import argparse
import sys

from app.config import settings
from app.storage.base import ObjectStore
from app.storage.local import LocalObjectStore


def _backend(name: str) -> ObjectStore:
    if name == "local":
        return LocalObjectStore(settings.data_dir / "blobs")
    if name == "s3":
        from app.storage.s3 import S3ObjectStore

        return S3ObjectStore(
            bucket=settings.s3_bucket,
            prefix="/".join(p for p in (settings.s3_prefix.strip("/"), "blobs") if p),
            endpoint=settings.s3_endpoint,
            region=settings.s3_region,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
            addressing=settings.s3_addressing,
            presign=False,
        )
    raise SystemExit(f"unknown backend {name!r} (want: local | s3)")


def run(dest_name: str, *, commit: bool, delete_source: bool) -> int:
    src_name = "local" if dest_name == "s3" else "s3"
    src, dest = _backend(src_name), _backend(dest_name)
    print(f"{src_name} -> {dest_name}   ({'COMMIT' if commit else 'dry run'})")

    copied = skipped = deleted = 0
    for key in src.iter_keys():
        if dest.exists(key):
            skipped += 1
            continue
        if not commit:
            copied += 1
            continue
        with src.open(key) as body:
            written = dest.put(key, body)
        if dest.size(key) != written:
            print(f"  !! size mismatch on {key} — leaving source in place", file=sys.stderr)
            continue
        copied += 1
        if delete_source:
            src.delete(key)
            deleted += 1
        if copied % 100 == 0:
            print(f"  … {copied} copied")

    verb = "copied" if commit else "would copy"
    print(f"done: {verb} {copied}, already present {skipped}, source deleted {deleted}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.storage.migrate")
    p.add_argument("--to", required=True, choices=("local", "s3"), help="destination backend")
    p.add_argument("--commit", action="store_true", help="actually write (default: dry run)")
    p.add_argument(
        "--delete-source",
        action="store_true",
        help="remove each source blob after its copy is verified",
    )
    args = p.parse_args(argv)
    return run(args.to, commit=args.commit, delete_source=args.delete_source)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
