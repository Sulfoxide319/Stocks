#!/usr/bin/env python3
"""Runtime dependency bootstrap for the local stock toolkit."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path


REQUIRED_MODULES = {
    "requests": "requests",
    "baostock": "baostock",
}


def missing_modules() -> list[str]:
    return [module for module in REQUIRED_MODULES if importlib.util.find_spec(module) is None]


def ensure_project_dependencies(requirements: Path | None = None) -> None:
    if os.getenv("STOCKS_SKIP_AUTO_INSTALL") == "1":
        missing = missing_modules()
        if missing:
            packages = ", ".join(REQUIRED_MODULES[module] for module in missing)
            raise SystemExit(f"Missing dependencies and auto install is disabled: {packages}")
        return

    missing = missing_modules()
    if not missing:
        return

    root = Path(__file__).resolve().parent
    requirements_path = requirements or root / "requirements.txt"
    if not requirements_path.exists():
        packages = ", ".join(REQUIRED_MODULES[module] for module in missing)
        raise SystemExit(f"Missing dependencies: {packages}; requirements.txt not found")

    print(
        "Installing missing Python dependencies: "
        + ", ".join(REQUIRED_MODULES[module] for module in missing),
        file=sys.stderr,
    )
    command = [sys.executable, "-m", "pip", "install", "-r", str(requirements_path)]
    result = subprocess.run(command, cwd=root, text=True)
    if result.returncode != 0:
        raise SystemExit(f"Dependency install failed: {' '.join(command)}")

    still_missing = missing_modules()
    if still_missing:
        packages = ", ".join(REQUIRED_MODULES[module] for module in still_missing)
        raise SystemExit(f"Dependencies still missing after install: {packages}")
