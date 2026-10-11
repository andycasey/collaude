import importlib.util
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("collaude", ROOT / "plugins/collaude/bin/collaude.py")
cl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cl)

ADMIN_DSN = os.environ.get("COLLAUDE_TEST_ADMIN_DSN")
needs_db = pytest.mark.skipif(not ADMIN_DSN, reason="COLLAUDE_TEST_ADMIN_DSN not set")


def entry(kind, content, **extra):
    return json.dumps({"type": kind, "message": {"role": kind, "content": content}, **extra}) + "\n"
