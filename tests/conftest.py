import sys
from pathlib import Path

# Run the suite straight from a checkout, without installing the package first.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
