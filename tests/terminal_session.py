"""Reproducible UI fixture. Docker and speed measurements are simulated."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import install


def session(**options):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        facts = {"hostname": "example-node", "system": "Linux", "installation": {"directory": "/opt/stolas", "config": False},
                 "docker": {"available": True, "version": "test"}, "stolas": [], "addresses": [], "n8n": [], "warnings": []}
        if options.get("reuse"):
            cfg = copy.deepcopy(install.DEFAULT)
            cfg["route"]["expected_public_cidrs"] = ["192.0.2.1/32"]
            (root / "config").mkdir()
            (root / "config/local.json").write_text(json.dumps(cfg))
            (root / ".env").write_text("STOLAS_API_TOKEN=synthetic-fixture-token-000000000000\nSTOLAS_LISTEN=127.0.0.1\nSTOLAS_PORT=8080\n")
        def command(args, root, capture=False, check=True, **kwargs):
            return subprocess.CompletedProcess(args, 0, json.dumps({"status": "ok", "primary": None, "confirmation": None}), "")
        with patch.object(install.environment, "discover", return_value=facts), patch.object(install.environment, "port_state", return_value="free"), patch.object(install, "docker_command", return_value=["docker", "compose"]), patch.object(install, "run", side_effect=command), patch.object(install, "request_json", return_value={"status": "ready"}):
            return install.install(root, **options)


if __name__ == "__main__":
    sys.exit(session())
