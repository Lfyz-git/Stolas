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


def session():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        facts = {"hostname": "example-node", "system": "Linux", "installation": {"directory": "/opt/stolas", "config": False},
                 "docker": {"available": True, "version": "test"}, "stolas": [], "addresses": [], "n8n": [], "warnings": []}
        def command(args, root, capture=False, check=True):
            return subprocess.CompletedProcess(args, 0, json.dumps({"status": "ok", "primary": None, "confirmation": None}), "")
        with patch.object(install.environment, "discover", return_value=facts), patch.object(install.environment, "port_state", return_value="free"), patch.object(install, "docker_command", return_value=["docker", "compose"]), patch.object(install, "run", side_effect=command), patch.object(install, "request_json", return_value={"status": "ready"}):
            return install.install(root)


if __name__ == "__main__":
    sys.exit(session())
