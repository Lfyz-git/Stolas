"""Management UI fixture; real terminal, isolated files and fake Docker."""
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import deploy, install, manage, resources
from test_runtime import DockerFixture

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory) / "stolas"
    root.mkdir()
    deploy.stage_sources(Path(__file__).resolve().parents[1], root, "v0.5.0", "fixture")
    (root / ".env").write_text("STOLAS_API_TOKEN=" + "a" * 40 + "\nSTOLAS_PORT=8080\n")
    (root / "config").mkdir()
    (root / "config/local.json").write_text(json.dumps(install.DEFAULT))
    docker = DockerFixture()
    with patch.object(resources.environment, "command", side_effect=docker), patch.object(resources, "command", return_value=["docker"]):
        resources.configure(root, ["docker"])
        manage.uninstall(root)
    facts = {"installation": {"ref": "v0.5.0", "config": True}, "docker": {"available": True}}
    with patch.object(sys, "argv", ["stolas", "--root", str(root), "diagnose"]), patch.object(manage.environment, "discover", return_value=facts):
        sys.exit(manage.main())
