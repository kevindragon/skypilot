"""Unit tests for the RustFS adaptor."""

import os
import unittest
from unittest import mock

from sky import exceptions
from sky.adaptors import rustfs
from sky.clouds import cloud
from sky.data import data_utils
from sky.data import mounting_utils
from sky.data import storage as storage_lib


class TestRustFSAdaptor(unittest.TestCase):
    """Test cases for the RustFS adaptor functions."""

    def setUp(self):
        # Make sure no environment variable leaks into a test.
        self._env_patcher = mock.patch.dict(os.environ, {}, clear=False)
        self._env_patcher.start()
        for name in ('RUSTFS_ENDPOINT', 'RUSTFS_ACCESS_KEY',
                     'RUSTFS_SECRET_KEY', 'RUSTFS_REGION'):
            os.environ.pop(name, None)
        self.addCleanup(self._env_patcher.stop)

    def test_get_endpoint_from_config(self):
        with mock.patch('sky.adaptors.rustfs._get_config',
                        return_value='http://10.0.0.10:7112'):
            self.assertEqual(rustfs.get_endpoint(), 'http://10.0.0.10:7112')

    def test_get_endpoint_strips_trailing_slash(self):
        with mock.patch('sky.adaptors.rustfs._get_config',
                        return_value='http://10.0.0.10:7112/'):
            self.assertEqual(rustfs.get_endpoint(), 'http://10.0.0.10:7112')

    def test_get_endpoint_env_var_takes_precedence(self):
        os.environ['RUSTFS_ENDPOINT'] = 'http://192.168.1.1:7112'
        with mock.patch('sky.adaptors.rustfs._get_config',
                        return_value='http://10.0.0.10:7112'):
            self.assertEqual(rustfs.get_endpoint(), 'http://192.168.1.1:7112')

    def test_get_endpoint_none_when_not_configured(self):
        with mock.patch('sky.adaptors.rustfs._get_config', return_value=None):
            self.assertIsNone(rustfs.get_endpoint())

    def test_get_endpoint_url_raises_when_not_configured(self):
        with mock.patch('sky.adaptors.rustfs._get_config', return_value=None):
            with self.assertRaises(ValueError):
                rustfs.get_endpoint_url()

    def test_get_region_default(self):
        with mock.patch('sky.adaptors.rustfs._get_config', return_value=None):
            self.assertEqual(rustfs.get_region(), rustfs.DEFAULT_REGION)

    def test_get_region_from_config(self):
        with mock.patch('sky.adaptors.rustfs._get_config',
                        return_value='cn-north-1'):
            self.assertEqual(rustfs.get_region(), 'cn-north-1')

    def test_ensure_credentials_file_writes_profile(self):
        with mock.patch('sky.adaptors.rustfs.get_access_key',
                        return_value='ak'), \
             mock.patch('sky.adaptors.rustfs.get_secret_key',
                        return_value='sk'), \
             mock.patch('sky.adaptors.rustfs.RUSTFS_CREDENTIALS_PATH',
                        '~/.rustfs/test-credentials'):
            path = rustfs.ensure_credentials_file()
            self.assertIsNotNone(path)
            assert path is not None
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
            self.assertIn(f'[{rustfs.RUSTFS_PROFILE_NAME}]', content)
            self.assertIn('aws_access_key_id = ak', content)
            self.assertIn('aws_secret_access_key = sk', content)
            os.unlink(path)

    def test_ensure_credentials_file_none_without_credentials(self):
        with mock.patch('sky.adaptors.rustfs.get_access_key',
                        return_value=None):
            self.assertIsNone(rustfs.ensure_credentials_file())

    def test_check_storage_credentials_missing_endpoint(self):
        with mock.patch('sky.adaptors.rustfs.get_endpoint', return_value=None):
            result, hints = rustfs.check_storage_credentials()
        self.assertFalse(result)
        assert hints is not None
        self.assertIn('endpoint', hints.lower())

    def test_check_storage_credentials_missing_credentials(self):
        with mock.patch('sky.adaptors.rustfs.get_endpoint',
                        return_value='http://10.0.0.10:7112'), \
             mock.patch('sky.adaptors.rustfs.get_access_key',
                        return_value=None):
            result, hints = rustfs.check_storage_credentials()
        self.assertFalse(result)
        assert hints is not None
        self.assertIn('credential', hints.lower())

    def test_check_storage_credentials_success(self):
        with mock.patch('sky.adaptors.rustfs.get_endpoint',
                        return_value='http://10.0.0.10:7112'), \
             mock.patch('sky.adaptors.rustfs.get_access_key',
                        return_value='ak'), \
             mock.patch('sky.adaptors.rustfs.get_secret_key',
                        return_value='sk'), \
             mock.patch('sky.adaptors.rustfs.ensure_credentials_file',
                        return_value='/tmp/credentials'), \
             mock.patch('sky.adaptors.rustfs.client'):
            result, hints = rustfs.check_storage_credentials()
        self.assertTrue(result)
        self.assertIsNone(hints)

    def test_check_credentials_unsupported_capability(self):
        with self.assertRaises(exceptions.NotSupportedError):
            rustfs.check_credentials(cloud.CloudCapability.COMPUTE)

    def test_get_credential_file_mounts(self):
        mounts = rustfs.get_credential_file_mounts()
        self.assertEqual(mounts, {
            rustfs.RUSTFS_CREDENTIALS_PATH: rustfs.RUSTFS_CREDENTIALS_PATH,
        })

    def test_client_uses_path_style_and_endpoint(self):
        mock_session = mock.Mock()
        with mock.patch('sky.adaptors.rustfs.session',
                        return_value=mock_session), \
             mock.patch('sky.adaptors.rustfs.get_endpoint_url',
                        return_value='http://10.0.0.10:7112'):
            rustfs.client('s3')

        mock_session.client.assert_called_once()
        call_args = mock_session.client.call_args
        self.assertEqual(call_args[0][0], 's3')
        self.assertEqual(call_args[1]['endpoint_url'], 'http://10.0.0.10:7112')
        self.assertEqual(call_args[1]['config'].s3['addressing_style'], 'path')


class TestRustFSStorage(unittest.TestCase):
    """Test cases for the RustFS storage integration."""

    def test_split_rustfs_path(self):
        self.assertEqual(data_utils.split_rustfs_path('rustfs://bucket'),
                         ('bucket', ''))
        self.assertEqual(data_utils.split_rustfs_path('rustfs://bucket/train/'),
                         ('bucket', 'train/'))

    def test_store_type_is_registered(self):
        self.assertEqual(storage_lib.StoreType.RUSTFS.value, 'RUSTFS')
        self.assertEqual(storage_lib.StoreType.RUSTFS.store_prefix(),
                         'rustfs://')

    def test_store_type_from_url(self):
        (store_type, bucket, sub_path, _,
         _) = (storage_lib.StoreType.get_fields_from_store_url(
             'rustfs://mybucket/some/prefix'))
        self.assertEqual(store_type, storage_lib.StoreType.RUSTFS)
        self.assertEqual(bucket, 'mybucket')
        self.assertEqual(sub_path, 'some/prefix')

    def test_store_type_from_cloud(self):
        self.assertEqual(storage_lib.StoreType.from_cloud('rustfs'),
                         storage_lib.StoreType.RUSTFS)

    def test_supported_storage_schemes_include_rustfs(self):
        schemes = storage_lib._get_supported_storage_schemes()  # pylint: disable=protected-access
        self.assertIn('rustfs', schemes)
        # The pre-existing schemes must keep working.
        for scheme in ('s3', 'gs', 'https', 'r2', 'cos', 'oci', 'nebius', 'cw',
                       'vastdata', 'hf'):
            self.assertIn(scheme, schemes)

    def test_validate_source_accepts_rustfs(self):
        """A rustfs:// source must pass source validation.

        Regression test for the hardcoded scheme whitelist that rejected
        rustfs:// with 'Supported paths: ...' before the store registry was
        consulted.
        """
        (source, is_local_source) = storage_lib.Storage._validate_source(  # pylint: disable=protected-access
            'rustfs://test-sky', storage_lib.StorageMode.MOUNT, True)
        self.assertEqual(source, 'rustfs://test-sky')
        self.assertFalse(is_local_source)

    def test_validate_source_rejects_unknown_scheme(self):
        with self.assertRaises(exceptions.StorageSourceError):
            storage_lib.Storage._validate_source(  # pylint: disable=protected-access
                'notastore://bucket', storage_lib.StorageMode.MOUNT, True)

    def test_mount_cmd_contains_endpoint_and_path_style(self):
        cmd = mounting_utils.get_rustfs_mount_cmd(
            rustfs.RUSTFS_CREDENTIALS_PATH,
            rustfs.RUSTFS_PROFILE_NAME,
            'http://10.0.0.10:7112',
            'mybucket',
            '/mnt/rustfs',
            region='us-east-1')
        # goofys (x86_64) gets the endpoint; rclone (arm64) gets
        # --s3-endpoint and the path-style flag.
        self.assertIn('--endpoint http://10.0.0.10:7112', cmd)
        self.assertIn('--s3-endpoint http://10.0.0.10:7112', cmd)
        self.assertIn('--s3-force-path-style=true', cmd)
        # The endpoint must not be passed in the virtual-hosted style.
        self.assertNotIn('--subdomain', cmd)
        self.assertIn('AWS_PROFILE=rustfs', cmd)

    def test_mount_cmd_read_only(self):
        cmd = mounting_utils.get_rustfs_mount_cmd(
            rustfs.RUSTFS_CREDENTIALS_PATH,
            rustfs.RUSTFS_PROFILE_NAME,
            'http://10.0.0.10:7112',
            'mybucket',
            '/mnt/rustfs',
            read_only=True)
        self.assertIn('--read-only', cmd)
        self.assertIn('-o ro', cmd)

    def test_rclone_profile_has_endpoint_and_path_style(self):
        with mock.patch('sky.adaptors.rustfs.get_endpoint_url',
                        return_value='http://10.0.0.10:7112'), \
             mock.patch('sky.adaptors.rustfs.get_access_key',
                        return_value='ak'), \
             mock.patch('sky.adaptors.rustfs.get_secret_key',
                        return_value='sk'), \
             mock.patch('sky.adaptors.rustfs.get_region',
                        return_value='us-east-1'):
            config = data_utils.Rclone.RcloneStores.RUSTFS.get_config(
                rclone_profile_name='sky-rustfs-mybucket')
        self.assertIn('[sky-rustfs-mybucket]', config)
        self.assertIn('endpoint = http://10.0.0.10:7112', config)
        self.assertIn('force_path_style = true', config)
        self.assertIn('access_key_id = ak', config)


if __name__ == '__main__':
    unittest.main()
