"""Deployment readiness and explicit measurement have separate entry points."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from agent import __main__ as agent_main
from agent.api import make_server
ROOT = Path(__file__).resolve().parents[1]
HAS_TOOLS = (ROOT / "tools/manage.py").is_file()
if HAS_TOOLS:
    from tools import manage


class SeparationTests(unittest.TestCase):
    def test_container_start_and_restart_never_measure(self):
        # Deployment manifests exist in the source tree, not the runtime image.
        # Verify their contents on the host; always exercise serve behavior.
        if (ROOT / 'Dockerfile').is_file():
            self.assertIn('CMD ["serve"]', (ROOT / 'Dockerfile').read_text())
        if (ROOT / 'compose.yaml').is_file():
            self.assertNotIn('command:', (ROOT / 'compose.yaml').read_text())
        with tempfile.TemporaryDirectory() as data:
            for _ in range(2):
                server = Mock()
                with patch('sys.argv', ['agent', 'serve', '--data', data]), patch.object(agent_main, 'make_server', return_value=server), patch.object(agent_main.signal, 'signal'), patch.object(agent_main.Runner, 'run', side_effect=AssertionError('measurement forbidden')) as run, patch('agent.probes.guard', side_effect=AssertionError('WAN forbidden')) as guard, patch('agent.probes.measure', side_effect=AssertionError('load forbidden')) as measure:
                    self.assertEqual(agent_main.main(), 0)
                server.serve_forever.assert_called_once()
                server.server_close.assert_called_once()
                run.assert_not_called()
                guard.assert_not_called()
                measure.assert_not_called()

    def test_health_readiness_and_authorization_never_measure(self):
        runner = Mock()
        token = 'a' * 40
        with patch('agent.api.BoundedServer') as server:
            make_server(runner, ('127.0.0.1', 0), token)
        handler_class = server.call_args.args[1]
        handler = object.__new__(handler_class)
        handler.path = '/healthz'
        handler.reply = Mock()
        for authorization, status in (('Bearer ' + token, 200), ('Bearer wrong', 401)):
            handler.headers = {'Authorization': authorization}
            handler.do_GET()
            self.assertEqual(handler.reply.call_args.args[0], status)
        runner.run.assert_not_called()
        runner.store.assert_not_called()

    def test_explicit_agent_run_executes_one_measurement(self):
        result = {'status': 'ok'}
        with tempfile.TemporaryDirectory() as data, patch('sys.argv', ['agent', 'run', '--data', data]), patch.object(agent_main.Runner, 'run', return_value=result) as run, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(agent_main.main(), 0)
        run.assert_called_once_with()
        self.assertEqual(json.loads(output.getvalue()), result)

    @unittest.skipUnless(HAS_TOOLS, "Host management outside Core image")
    def test_explicit_human_cli_run_executes_one_measurement(self):
        with tempfile.TemporaryDirectory() as data, patch('sys.argv', ['stolas', '--root', data, 'run']), patch.object(manage.resources, 'command', return_value=['docker']), patch.object(manage.resources, 'compose', return_value=['docker', 'compose']), patch.object(manage, 'first_test', return_value={'status': 'ok'}) as run:
            self.assertEqual(manage.main(), 0)
        run.assert_called_once()
