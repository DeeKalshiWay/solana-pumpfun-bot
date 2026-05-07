"""Shared pytest setup. Ensures `logs/` exists so module-level singletons
(creator_tracker, etc.) that auto-create state files don't fail at import."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.makedirs(ROOT / "logs", exist_ok=True)
