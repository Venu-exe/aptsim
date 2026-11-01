"""Deterministic synthetic lab generator.

Creates a disposable victim tree with clearly fake documents and credentials,
copies the sample stage plugin into ``<lab>/plugins`` and writes a lab README.
The random binary blob is generated from a fixed seed so runs are reproducible.
"""

from __future__ import annotations

import random
import shutil
from pathlib import Path

from ..core.paths import LabPaths

TEXT_FILES: dict[str, str] = {
    "documents/notes.txt": (
        "SYNTHETIC LAB DATA - NOT REAL\n"
        "Project: aptsim detection engineering lab\n"
        "Owner: blue-team training\n"
        "Reminder: every file in this tree is disposable.\n"
    ),
    "documents/quarterly-report.txt": (
        "SYNTHETIC LAB DATA - NOT REAL\n"
        "Quarterly summary\n"
        "- revenue: 100 (fictional)\n"
        "- incidents: 2 (fictional)\n"
        "- action items: build detection content\n"
    ),
    "finance/invoices/invoice-2026-001.txt": (
        "SYNTHETIC LAB DATA - NOT REAL\n"
        "Invoice 2026-001\n"
        "Vendor: Example Labs\n"
        "Amount: 42.00 EUR\n"
    ),
    "finance/payroll-export.csv": (
        "employee_id,name,gross,currency\n"
        "LAB-001,Ada Example,1000,EUR\n"
        "LAB-002,Grace Example,1200,EUR\n"
        "# synthetic payroll export for lab exercises only\n"
    ),
    "projects/readme.md": (
        "# Lab project\n\n"
        "This tree exists so file-discovery, hashing and reversible transforms\n"
        "have something realistic to chew on.\n"
    ),
    "credentials/lab-credentials.txt": (
        "SYNTHETIC LAB CREDENTIALS - NOT VALID ANYWHERE\n"
        "aws_access_key_id = AKIA-LAB-FAKE-0001\n"
        "aws_secret_access_key = labfake/0000000000000000000000000000\n"
        "ssh_user = labuser\n"
        "ssh_password = not-a-real-password\n"
        "note: generated for detection content, never use against real systems\n"
    ),
}

LAB_README = """# aptsim disposable lab

This directory is generated and owned by the aptsim framework.

* `victim/` contains synthetic files only. It is the only writable target for
  the simulation modules.
* `plugins/` contains local stage plugins loaded at runtime.
* `persistence/` contains simulated startup artifacts. Nothing here is
  installed on the host.
* `sessions/` contains immutable event logs, state checkpoints and manifests.
* `.backup/` holds reversible-transform backups and is removed by cleanup.

Delete this entire directory to reset the lab. Do not copy anything from here
to a real system.
"""


def populate(paths: LabPaths, *, seed: int = 20261031, overwrite: bool = False) -> dict[str, object]:
    paths.ensure_layout()
    created: list[str] = []

    for relative, content in TEXT_FILES.items():
        target = paths.victim / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if overwrite or not target.exists():
            target.write_text(content, encoding="utf-8")
            created.append(str(target.relative_to(paths.root)))

    archive = paths.victim / "archive" / "data.bin"
    archive.parent.mkdir(parents=True, exist_ok=True)
    if overwrite or not archive.exists():
        rng = random.Random(seed)
        archive.write_bytes(rng.randbytes(4096))
        created.append(str(archive.relative_to(paths.root)))

    plugin_source = Path(__file__).resolve().parent.parent / "plugins" / "sample_stage.py"
    plugin_target = paths.plugins / "sample_stage.py"
    if plugin_source.exists():
        shutil.copyfile(plugin_source, plugin_target)
        created.append(str(plugin_target.relative_to(paths.root)))

    readme = paths.root / "README.lab.md"
    readme.write_text(LAB_README, encoding="utf-8")

    return {
        "lab_root": str(paths.root),
        "victim": str(paths.victim),
        "created": created,
        "files": len(list(paths.victim.rglob("*"))),
    }
