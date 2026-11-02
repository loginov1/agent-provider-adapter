"""A CLI-only update must preserve the active provider implementation/config."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('operator_install', Path(__file__).resolve().parent.parent / 'install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class OperatorInstallTests(unittest.TestCase):
    def test_cli_update_clones_active_runtime_and_preserves_adapter_and_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base, source, state = root / 'runtime', root / 'source', root / 'backups'
            previous = base / 'versions/previous'
            previous.mkdir(parents=True)
            source.mkdir()
            for name, content in [('agent_jobs.py', 'old_cli = True\n'),
                                  ('agent_exec.py', 'provider_implementation = True\n'),
                                  ('policy.json', '{"keep": true}\n')]:
                (previous / name).write_text(content)
            (base / 'current').symlink_to(previous)
            (source / 'agent_jobs.py').write_text('new_cli = True\n')
            with patch.object(installer, 'BASE', base), patch.object(installer, 'SOURCE', source), \
                    patch.object(installer, 'INSTALL_STATE', state), patch.object(installer, 'git', side_effect=['', 'checked-commit']):
                with patch.object(installer.os, 'chown'):
                    installer.install_operator_cli()
            installed = (base / 'current').resolve()
            self.assertNotEqual(installed, previous)
            self.assertEqual((installed / 'agent_jobs.py').read_text(), 'new_cli = True\n')
            for name in ['agent_exec.py', 'policy.json']:
                self.assertEqual((installed / name).read_bytes(), (previous / name).read_bytes())
            self.assertEqual((previous / 'agent_jobs.py').read_text(), 'old_cli = True\n')
            self.assertEqual(len(list(state.glob('install-*/manifest.json'))), 1)


if __name__ == '__main__':
    unittest.main()
