"""Require exact Product checkouts for integration tests. | 强制使用指定产品检出。"""

import os
import sys
from pathlib import Path

for repository, source in (
    ("CATALYST", "src"),
    ("YIELD", "training/core/src"),
    ("REACTOR", "product/src"),
    ("NAVIGATOR", "src"),
    ("ECHO", "src"),
):
    selected = Path(os.environ[f"CYRENE_{repository}_WORKTREE"]).resolve() / source
    if not selected.is_dir():
        raise RuntimeError("Missing exact Product checkout: " + repository)
    sys.path.insert(0, str(selected))
