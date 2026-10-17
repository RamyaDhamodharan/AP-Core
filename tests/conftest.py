
import sys
from pathlib import Path

CORE_ROOT = Path(__file__).resolve().parents[1]

if str(CORE_ROOT) not in sys.path:
    sys.path.insert(0, str(CORE_ROOT))
