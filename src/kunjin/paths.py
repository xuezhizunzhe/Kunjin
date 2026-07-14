from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RuntimePaths:
    database: Path
    snapshots: Path
    logs: Path

    @classmethod
    def from_environment(cls) -> "RuntimePaths":
        home = Path.home()
        data_dir = Path(
            os.environ.get("KUNJIN_DATA_DIR", home / ".local" / "share" / "kunjin")
        ).expanduser()
        state_dir = Path(
            os.environ.get("KUNJIN_STATE_DIR", home / ".local" / "state" / "kunjin")
        ).expanduser()
        return cls(
            database=data_dir / "kunjin.db",
            snapshots=data_dir / "snapshots",
            logs=state_dir / "logs",
        )

    @property
    def imports(self) -> Path:
        return self.database.parent / "imports"

    @property
    def fund_documents(self) -> Path:
        return self.database.parent / "fund-documents"

    @property
    def legacy_doc_runtime(self) -> Path:
        return self.logs.parent / "legacy-doc-runtime"

    def ensure(self) -> "RuntimePaths":
        for directory in (
            self.database.parent,
            self.snapshots,
            self.imports,
            self.fund_documents,
            self.logs,
            self.legacy_doc_runtime,
        ):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory.chmod(0o700)
        return self
