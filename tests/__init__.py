"""Point every test at the fixture company config before app.settings loads."""
import os
from pathlib import Path

os.environ.setdefault("BRIDGE_CONFIG", str(Path(__file__).parent / "fixtures" / "company.test.json"))
