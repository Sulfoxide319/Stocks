#!/usr/bin/env python3
"""Audit a Stocks Trading Assistant release zip for packaging regressions."""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path


FORBIDDEN_RELATIVE_PATHS = {
    "config/broker_account_snapshot.json",
    "config/live_positions.csv",
    "config/ui_settings.json",
    "config/xueqiu_cookie.txt",
}

REQUIRED_ENTRIES = {
    "app/StocksTradingAssistant.exe",
    "Install-StocksTool.ps1",
    "Start-TradingAssistant.bat",
    "Update-StocksTool.ps1",
    "VERSION",
    "release.json",
    "update_manifest.json",
}


def normalize_zip_path(value: str) -> str:
    return value.replace("\\", "/").lstrip("/")


def read_zip_text(package: zipfile.ZipFile, name: str) -> str:
    with package.open(name) as handle:
        return handle.read().decode("utf-8-sig")


def audit_package(zip_path: Path, expected_version: str = "") -> list[str]:
    errors: list[str] = []
    with zipfile.ZipFile(zip_path) as package:
        entries = {normalize_zip_path(info.filename) for info in package.infolist()}
        missing = sorted(REQUIRED_ENTRIES - entries)
        if missing:
            errors.append("missing required entries: " + ", ".join(missing))

        forbidden_entries = sorted(FORBIDDEN_RELATIVE_PATHS & entries)
        if forbidden_entries:
            errors.append("forbidden user-owned files in zip: " + ", ".join(forbidden_entries))

        try:
            manifest = json.loads(read_zip_text(package, "update_manifest.json"))
        except Exception as exc:  # pragma: no cover - defensive CLI reporting.
            errors.append(f"cannot read update_manifest.json: {exc}")
            return errors

        manifest_files = manifest.get("files", [])
        if not isinstance(manifest_files, list):
            errors.append("update_manifest.json files must be a list")
            manifest_files = []

        forbidden_targets = sorted(
            {
                normalize_zip_path(str(item.get("target", "")))
                for item in manifest_files
                if isinstance(item, dict)
            }
            & FORBIDDEN_RELATIVE_PATHS
        )
        if forbidden_targets:
            errors.append("forbidden user-owned targets in manifest: " + ", ".join(forbidden_targets))

        version = str(manifest.get("version", "")).strip()
        if expected_version and version != expected_version:
            errors.append(f"manifest version {version!r} != expected {expected_version!r}")

        try:
            version_text = read_zip_text(package, "VERSION").strip()
        except Exception as exc:  # pragma: no cover - defensive CLI reporting.
            errors.append(f"cannot read VERSION: {exc}")
            version_text = ""
        if expected_version and version_text != expected_version:
            errors.append(f"VERSION {version_text!r} != expected {expected_version!r}")

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit a release zip before publishing.")
    parser.add_argument("zip_path", type=Path)
    parser.add_argument("--expected-version", default="")
    args = parser.parse_args(argv)

    errors = audit_package(args.zip_path, args.expected_version)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"ok {args.zip_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
