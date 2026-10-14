import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from config.settings import _corridor_miles, _load_dotenv, _parse_dotenv_line


class CorridorSettingTests(SimpleTestCase):
    def test_valid_values(self):
        for raw, expected in (('10', 10.0), ('0.5', 0.5), (' 25 ', 25.0), ('1e1', 10.0)):
            with self.subTest(raw=raw):
                self.assertEqual(_corridor_miles(raw), expected)

    def test_invalid_values_raise_improperly_configured(self):
        for raw in ('abc', '', '0', '0.0', '-1', 'nan', 'inf', '-inf', None):
            with self.subTest(raw=raw), self.assertRaises(ImproperlyConfigured) as ctx:
                _corridor_miles(raw)
            self.assertIn('FUEL_CORRIDOR_MILES', str(ctx.exception))

    def test_active_setting_is_positive(self):
        self.assertGreater(settings.FUEL_CORRIDOR_MILES, 0)

    def test_django_refuses_to_start_with_invalid_value(self):
        project_root = Path(settings.BASE_DIR)
        env = {**os.environ, 'DJANGO_SETTINGS_MODULE': 'config.settings', 'DJANGO_DEBUG': 'true',
               'DJANGO_SECRET_KEY': 'test-only', 'PYTHONPATH': str(project_root)}
        script = 'import django; django.setup(); from django.conf import settings; print(settings.FUEL_CORRIDOR_MILES)'
        for raw, ok in (('abc', False), ('0', False), ('-3', False), ('7.5', True)):
            with self.subTest(raw=raw):
                result = subprocess.run([sys.executable, '-c', script], cwd=project_root, capture_output=True,
                                        text=True, timeout=60, env={**env, 'FUEL_CORRIDOR_MILES': raw})
                if ok:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), '7.5')
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('ImproperlyConfigured', result.stderr)
                    self.assertIn('FUEL_CORRIDOR_MILES', result.stderr)


class DotenvParsingTests(SimpleTestCase):
    def test_parse_lines(self):
        cases = {
            'KEY=value': ('KEY', 'value'),
            '  KEY  =  value with spaces  ': ('KEY', 'value with spaces'),
            'KEY="double quoted"': ('KEY', 'double quoted'),
            "KEY='single quoted'": ('KEY', 'single quoted'),
            'KEY=" padded inside "': ('KEY', ' padded inside '),
            'KEY=""': ('KEY', ''),
            'KEY=': ('KEY', ''),
            'KEY="mismatched\'': ('KEY', '"mismatched\''),
            'KEY="unterminated': ('KEY', '"unterminated'),
            'KEY=it\'s "fine"': ('KEY', 'it\'s "fine"'),
            'KEY=a=b==c': ('KEY', 'a=b==c'),
            'KEY=abc=': ('KEY', 'abc='),
            'export KEY=value': ('KEY', 'value'),
            'export   KEY="quoted"': ('KEY', 'quoted'),
            'KEY=value # not a comment': ('KEY', 'value # not a comment'),
        }
        for line, expected in cases.items():
            with self.subTest(line=line):
                self.assertEqual(_parse_dotenv_line(line), expected)

    def test_ignored_lines(self):
        for line in ('', '   ', '# comment', '   # KEY=value', 'no equals sign', '=value', '  = value',
                     'TWO WORDS=value', 'export', 'export # comment'):
            with self.subTest(line=line):
                self.assertIsNone(_parse_dotenv_line(line))

    def test_load_does_not_override_real_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env'
            path.write_text(
                '# comment\nDOTENV_TEST_EXISTING=from-file\nexport DOTENV_TEST_NEW="from file"\n\nbroken line\n'
            )
            with mock.patch.dict(os.environ, {'DOTENV_TEST_EXISTING': 'from-environment'}):
                os.environ.pop('DOTENV_TEST_NEW', None)
                _load_dotenv(path)
                self.assertEqual(os.environ['DOTENV_TEST_EXISTING'], 'from-environment')
                self.assertEqual(os.environ['DOTENV_TEST_NEW'], 'from file')
                self.assertNotIn('broken line', os.environ)
        self.assertNotIn('DOTENV_TEST_NEW', os.environ)  # patch.dict restored the environment

    def test_missing_file_is_ignored(self):
        _load_dotenv(Path(tempfile.gettempdir()) / 'definitely-not-here' / '.env')
