import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('kd', Path(__file__).resolve().parents[1] / 'kd.py')
kd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kd)


class PrivacyTests(unittest.TestCase):
    def test_verbose_login_does_not_log_credentials_or_response(self):
        for auth in ({'password': 'fake-password'},
                     {'app_id': 'fake-app', 'app_secret': 'fake-secret'}):
            with self.subTest(auth=list(auth)), tempfile.TemporaryDirectory() as tmp:
                cfg = dict(url='https://private.example.invalid/', _profile='test',
                           acct_id='fake-account', username='fake-user', lcid=2052, **auth)
                response = b'{"LoginResultType":1,"Context":{"UserId":"private-response"}}'
                class Response:
                    def __enter__(self): return self
                    def __exit__(self, *args): pass
                    def read(self): return response
                with patch.object(kd, 'CONFIG_DIR', tmp):
                    client = kd.Client(cfg, verbose=True)
                output = io.StringIO()
                with patch.object(client.opener, 'open', return_value=Response()) as request, contextlib.redirect_stderr(output):
                    client.login()
                # Credentials still reach the authentication endpoint, but not logs.
                body = json.loads(request.call_args.args[0].data)
                self.assertEqual(body.get('password', body.get('appSecret')),
                                 auth.get('password', auth.get('app_secret')))
                self.assertIn('POST', output.getvalue())
                for value in ['private.example.invalid', 'fake-account', 'fake-user',
                              'private-response', *auth.values()]:
                    self.assertNotIn(value, output.getvalue())

    def test_secret_prompts_do_not_use_echoing_input(self):
        with patch.object(kd, 'load_config_file', return_value={'profiles': {}}), \
             patch.object(kd, 'save_config_file') as save, \
             patch('builtins.input', side_effect=['https://example.invalid/', 'account', 'user', 'app', '2052']) as normal, \
             patch.object(kd.getpass, 'getpass', side_effect=['fake-secret', 'fake-password']) as secret, \
             contextlib.redirect_stdout(io.StringIO()):
            kd.cmd_config_init(SimpleNamespace(profile=None))
        self.assertEqual(normal.call_count, 5)
        self.assertEqual(secret.call_count, 2)
        cfg = save.call_args.args[0]['profiles']['default']
        self.assertEqual(cfg['app_secret'], 'fake-secret')
        self.assertEqual(cfg['password'], 'fake-password')


if __name__ == '__main__':
    unittest.main()
