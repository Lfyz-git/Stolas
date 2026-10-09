import json
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


@unittest.skipUnless((ROOT / "config" / "apk.lock.json").exists(), "Source artifact checks run outside production image")
class ArtifactTests(unittest.TestCase):
    def test_lock_consistency(self):
        lock = json.loads((ROOT / "config/apk.lock.json").read_text())
        for arch, packages in lock["architectures"].items():
            expected = "".join(p["sha256"] + " " + p["url"] + "\n" for p in packages)
            self.assertEqual((ROOT / ("config/apk-" + arch + ".lock")).read_text(), expected)
            self.assertIn("busybox-binsh", [p["name"] for p in packages])
            self.assertNotIn("yash-binsh", [p["name"] for p in packages])
            for p in packages:
                self.assertRegex(p["sha256"], r"^[0-9a-f]{64}$")

    def test_workflow_no_credentials_or_active_schedule(self):
        workflow = json.loads((ROOT / "n8n/stolas.json").read_text())
        self.assertFalse(workflow["active"])
        nodes = {n["name"]: n for n in workflow["nodes"]}
        self.assertEqual(nodes["Run Stolas"]["parameters"]["genericAuthType"], "httpHeaderAuth")
        for node in nodes.values():
            self.assertNotIn("credentials", node)
        for source, edges in workflow["connections"].items():
            self.assertIn(source, nodes)
            for branch in edges["main"]:
                for edge in branch:
                    self.assertIn(edge["node"], nodes)
