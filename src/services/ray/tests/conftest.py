# ndif/src/services/ray/tests/conftest.py
import sys
from pathlib import Path

# ensure src/ is on path so ndif_ray is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
