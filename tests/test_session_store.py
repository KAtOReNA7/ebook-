import importlib.util
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import session_store as store
from scripts.weread_session import renew

SESSION = {'vid': '123', 'deviceId': 'fixture-device', 'accessToken': 'fixture-access',
           'refreshToken': 'fixture-refresh'}


@unittest.skipUnless(importlib.util.find_spec('cryptography'), 'optional portable dependency not installed')
class PortableSessionTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / 'session.aesgcm'
        self.key = self.root / 'test.key'
        self.env = patch.dict(os.environ, {store.KEY_ENV: str(self.key)})
        self.env.start()
        self.addCleanup(self.env.stop)
        store.create_key()

    def test_round_trip_fresh_nonce_and_no_plaintext(self):
        store.save_session(self.path, SESSION)
        first = self.path.read_bytes()
        self.assertNotIn(SESSION['accessToken'].encode(), first)
        self.assertEqual(store.load_session(self.path), SESSION)
        store.save_session(self.path, SESSION, replace=True)
        self.assertNotEqual(first, self.path.read_bytes())
        self.assertEqual(store.load_session(self.path), SESSION)

    def test_ciphertext_tampering_and_wrong_key_fail_closed(self):
        store.save_session(self.path, SESSION)
        original = self.path.read_bytes()
        self.path.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
        with self.assertRaises(ValueError):
            store.load_session(self.path)
        self.path.write_bytes(original)
        with patch.dict(os.environ, {store.KEY_ENV: str(self.root / 'different.key')}):
            store.create_key()
            with self.assertRaises(ValueError):
                store.load_session(self.path)
        self.assertEqual(store.load_session(self.path), SESSION)

    def test_missing_key_cannot_overwrite_previous_session(self):
        store.save_session(self.path, SESSION)
        original = self.path.read_bytes()
        with patch.dict(os.environ, {store.KEY_ENV: ''}):
            with self.assertRaises(ValueError):
                store.save_session(self.path, {**SESSION, 'accessToken': 'new'}, replace=True)
        self.assertEqual(original, self.path.read_bytes())

    def test_explicit_creation_never_overwrites_key_or_session(self):
        with self.assertRaises(FileExistsError):
            store.create_key()
        store.save_session(self.path, SESSION)
        with self.assertRaises(FileExistsError):
            store.save_session(self.path, SESSION)
        with self.assertRaises(FileNotFoundError):
            store.save_session(self.root / 'lost.aesgcm', SESSION, replace=True)

    def test_renewal_preserves_encrypted_backend_and_identity(self):
        store.save_session(self.path, SESSION)
        with patch('scripts.weread_session.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = io.BytesIO(json.dumps(
                {'vid': '123', 'accessToken': 'renewed', 'refreshToken': 'new-refresh'}).encode())
            renewed = renew(self.path)
        self.assertEqual(renewed['deviceId'], SESSION['deviceId'])
        self.assertEqual(store.load_session(self.path)['accessToken'], 'renewed')
        self.assertTrue(self.path.read_bytes().startswith(store.MAGIC))

    def test_rejected_renewal_preserves_previous_file(self):
        store.save_session(self.path, SESSION)
        original = self.path.read_bytes()
        with patch('scripts.weread_session.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = io.BytesIO(b'{"vid":"456","accessToken":"other"}')
            with self.assertRaises(ValueError):
                renew(self.path)
        self.assertEqual(original, self.path.read_bytes())

    @unittest.skipUnless(os.name == 'posix', 'POSIX permissions')
    def test_world_readable_key_or_session_rejected(self):
        store.save_session(self.path, SESSION)
        self.key.chmod(0o644)
        with self.assertRaises(PermissionError):
            store.load_session(self.path)
        self.key.chmod(0o600)
        self.path.chmod(0o644)
        with self.assertRaises(PermissionError):
            store.load_session(self.path)

    @unittest.skipUnless(os.name == 'posix', 'POSIX symlinks')
    def test_symlink_key_rejected(self):
        store.save_session(self.path, SESSION)
        alias = self.root / 'alias.key'
        alias.symlink_to(self.key)
        with patch.dict(os.environ, {store.KEY_ENV: str(alias)}):
            with self.assertRaises(ValueError):
                store.load_session(self.path)


class BackendTests(unittest.TestCase):
    def test_plaintext_path_rejected(self):
        with self.assertRaises(ValueError):
            store.save_session('session.json', SESSION)

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI')
    def test_existing_dpapi_format_still_loads_and_renews(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'session.dpapi'
            path.write_bytes(store.protect(json.dumps(SESSION).encode()))
            self.assertEqual(store.load_session(path), SESSION)
            store.save_session(path, {**SESSION, 'accessToken': 'updated'}, replace=True)
            self.assertEqual(store.load_session(path)['accessToken'], 'updated')

    @unittest.skipUnless(os.name == 'posix', 'Linux must not attempt DPAPI fallback')
    def test_dpapi_on_linux_requires_new_authorization(self):
        with self.assertRaises(RuntimeError):
            store.protect(b'fixture')
