"""Test environment.

Set before any app module is imported: app.config reads the environment at
import time and refuses to start without a SECRET_KEY, which is the behaviour
we want in production.
"""

import os
import tempfile

os.environ.setdefault("SECRET_KEY", "test-secret-key-not-for-real-use")
os.environ.setdefault("PLACE_ORDERS", "false")
os.environ.setdefault(
    "DATABASE_PATH", os.path.join(tempfile.mkdtemp(prefix="prehrana-test-"), "test.db")
)
