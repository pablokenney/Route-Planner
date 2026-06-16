"""Ensure the repo root is importable so tests can `import backend.generator`."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
