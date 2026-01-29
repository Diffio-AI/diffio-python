import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
TESTS = os.path.join(ROOT, "tests")
if SRC not in sys.path:
    sys.path.insert(0, SRC)
if TESTS not in sys.path:
    sys.path.insert(0, TESTS)
