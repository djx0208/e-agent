import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from e_agent.config import ConfigError, ModelConfig, default_config_path, load_config, save_config

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('configure_deepseek', ROOT / 'scripts/configure_deepseek.py')
script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(script)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'e-agent/model.json'
        self.config = ModelConfig('https://api.deepseek.com', 'deepseek-flash', 'test-secret-not-real')

    def test_missing_file_allows_startup_and_env_override(self):
        empty = load_config(self.path, environ={})
        self.assertEqual(empty.api_key, '')
        with self.assertRaises(ConfigError):
            empty.validate()
        config = load_config(self.path, environ={'E_AGENT_BASE_URL': 'https://example.test/v1', 'E_AGENT_MODEL': 'test', 'E_AGENT_API_KEY': 'dummy'})
        config.validate()
        self.assertEqual(config.model, 'test')

    def test_private_roundtrip_and_environment_precedence(self):
        save_config(self.config, self.path)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        self.assertEqual(load_config(self.path, environ={}), self.config)
        override = load_config(self.path, environ={'E_AGENT_MODEL': 'different'})
        self.assertEqual(override.model, 'different')
        self.assertEqual(override.api_key, self.config.api_key)
        self.assertNotIn(self.config.api_key, repr(self.config))
        self.assertEqual(default_config_path({'XDG_CONFIG_HOME': self.directory.name}), self.path)

    def test_bad_file_errors_do_not_reveal_contents(self):
        self.path.parent.mkdir()
        for raw in ['bad test-secret-not-real', '{"api_key":123}', '{"unknown":"test-secret-not-real"}']:
            self.path.write_text(raw)
            self.path.chmod(0o600)
            with self.assertRaises(ConfigError) as caught:
                load_config(self.path, environ={})
            self.assertNotIn(self.config.api_key, str(caught.exception))

    def test_wide_permissions_and_symlinks_rejected(self):
        save_config(self.config, self.path)
        self.path.chmod(0o644)
        with self.assertRaises(ConfigError):
            load_config(self.path, environ={})
        self.path.chmod(0o600)
        linked = Path(self.directory.name) / 'linked.json'
        linked.symlink_to(self.path)
        with self.assertRaises(ConfigError):
            load_config(linked, environ={})
        with self.assertRaises(ConfigError):
            save_config(self.config, linked)
        parent_link = Path(self.directory.name) / 'parent-link'
        parent_link.symlink_to(self.path.parent, target_is_directory=True)
        with self.assertRaises(ConfigError):
            save_config(self.config, parent_link / 'other.json')
        self.assertFalse((self.path.parent / 'other.json').exists())

    def test_validation_avoids_echoing_secret_and_invalid_values(self):
        values = [ModelConfig('https://secret-user:secret-password@example.test', 'm', 'secret'), ModelConfig('https://example.test/chat/completions', 'm', 'secret'), ModelConfig('file:///secret', 'm', 'secret'), ModelConfig('https://example.test?secret=value', 'm', 'secret'), ModelConfig('https://example.test', 'm', 'secret\n'), ModelConfig('https://example.test', 'm', 'secret', float('nan'))]
        for config in values:
            with self.subTest(config=config), self.assertRaises(ConfigError) as caught:
                config.validate()
            self.assertNotIn('secret-password', str(caught.exception))
            self.assertNotIn('secret\n', str(caught.exception))

    def test_atomic_failure_keeps_old_file(self):
        save_config(self.config, self.path)
        old = self.path.read_bytes()
        with patch('e_agent.config.os.replace', side_effect=OSError('secret exception text')):
            with self.assertRaises(ConfigError) as caught:
                save_config(ModelConfig(self.config.base_url, 'other', 'new-secret'), self.path)
        self.assertEqual(self.path.read_bytes(), old)
        self.assertNotIn('secret exception text', str(caught.exception))
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])

    def test_interactive_script_and_preserve_existing_key(self):
        output = io.StringIO()
        with patch('builtins.input', side_effect=['', '']), patch.object(script.getpass, 'getpass', return_value=self.config.api_key), contextlib.redirect_stdout(output):
            self.assertEqual(script.main(['--config', str(self.path)]), 0)
        self.assertNotIn(self.config.api_key, output.getvalue())
        self.assertEqual(load_config(self.path, environ={}).model, 'deepseek-flash')
        with patch('builtins.input', side_effect=['', 'different']), patch.object(script.getpass, 'getpass', return_value=''), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(script.main(['--config', str(self.path)]), 0)
        self.assertEqual(load_config(self.path, environ={}).api_key, self.config.api_key)
        self.assertEqual(load_config(self.path, environ={}).model, 'different')

    def test_check_does_not_prompt_or_network(self):
        save_config(self.config, self.path)
        output = io.StringIO()
        with patch('builtins.input', side_effect=AssertionError('must not prompt')), patch('socket.create_connection', side_effect=AssertionError('must not connect')), contextlib.redirect_stdout(output):
            self.assertEqual(script.main(['--config', str(self.path), '--check']), 0)
        self.assertIn('尚未联网验证', output.getvalue())
        self.assertNotIn(self.config.api_key, output.getvalue())

    def test_repository_destination_and_cancel_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(script.main(['--config', str(ROOT / '.test-model-config.json')]), 2)
            with patch('builtins.input', side_effect=KeyboardInterrupt), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(script.main(['--config', str(self.path)]), 130)
        self.assertFalse(self.path.exists())
        self.assertFalse((ROOT / '.test-model-config.json').exists())

    def test_non_ascii_and_whitespace_keys_rejected_without_echo(self):
        for key in ['中文密钥', ' dummy', 'dummy ']:
            with self.subTest(kind='invalid-key'), self.assertRaises(ConfigError) as caught:
                ModelConfig('https://api.deepseek.com', 'deepseek-flash', key).validate()
            self.assertNotIn(key, str(caught.exception))

    def test_existing_bad_config_not_displayed(self):
        save_config(self.config, self.path)
        data = json.loads(self.path.read_text())
        data['base_url'] = 'https://user:private-password@example.test'
        self.path.write_text(json.dumps(data))
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output), patch('builtins.input', side_effect=AssertionError('bad config must not prompt')):
            self.assertEqual(script.main(['--config', str(self.path)]), 2)
        self.assertNotIn('private-password', output.getvalue())
        self.assertNotIn(self.config.api_key, output.getvalue())

    def test_existing_labels_redact_key(self):
        save_config(ModelConfig('https://api.deepseek.com', self.config.api_key, self.config.api_key), self.path)
        prompts = []
        def enter(prompt):
            prompts.append(prompt)
            return ''
        with patch('builtins.input', side_effect=enter), patch.object(script.getpass, 'getpass', return_value=''), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(script.main(['--config', str(self.path)]), 0)
        self.assertNotIn(self.config.api_key, ''.join(prompts))
        self.assertIn('[REDACTED]', ''.join(prompts))
