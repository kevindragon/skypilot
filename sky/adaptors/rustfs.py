"""RustFS adaptor for SkyPilot.

RustFS is a self-hosted, S3-compatible object storage server. It is reached
through a custom endpoint (e.g. http://10.0.0.10:7112) rather than the AWS
S3 endpoint, so it cannot be served by the built-in AWS S3 storage backend.

Configuration (first match wins):
  1. Environment variables: RUSTFS_ENDPOINT, RUSTFS_ACCESS_KEY,
     RUSTFS_SECRET_KEY, RUSTFS_REGION.
  2. ~/.sky/config.yaml:
       rustfs:
         endpoint: http://10.0.0.10:7112
         access_key: <access key>
         secret_key: <secret key>
         region: us-east-1

The credentials are materialized into an AWS-style credentials file so that
the mount helpers running on the remote node (goofys / rclone / aws cli) can
pick them up:
    ~/.rustfs/credentials
        [rustfs]
        aws_access_key_id = ...
        aws_secret_access_key = ...

Requests use path-style addressing (the bucket in the URL path, not the
hostname), which is what RustFS serves by default.
"""

import os
import threading
from typing import Any, Dict, Optional, Tuple

from sky import exceptions
from sky import sky_logging
from sky import skypilot_config
from sky.adaptors import common
from sky.clouds import cloud
from sky.utils import ux_utils

NAME = 'RustFS'
RUSTFS_PROFILE_NAME = 'rustfs'
RUSTFS_CREDENTIALS_PATH = '~/.rustfs/credentials'
DEFAULT_REGION = 'us-east-1'

_IMPORT_ERROR_MESSAGE = ('Failed to import dependencies for RustFS. '
                         'Try pip install "skypilot[rustfs]"')

boto3 = common.LazyImport('boto3', import_error_message=_IMPORT_ERROR_MESSAGE)
botocore = common.LazyImport('botocore',
                             import_error_message=_IMPORT_ERROR_MESSAGE)
_LAZY_MODULES = (boto3, botocore)

logger = sky_logging.init_logger(__name__)

_session_creation_lock = threading.RLock()


def _get_config(key: str) -> Optional[str]:
    """Gets a rustfs config value from ~/.sky/config.yaml."""
    value = skypilot_config.get_nested(('rustfs', key), default_value=None)
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def get_endpoint() -> Optional[str]:
    """Returns the RustFS S3 endpoint, e.g. http://10.0.0.10:7112.

    Returns None when the endpoint is not configured.
    """
    endpoint = os.environ.get('RUSTFS_ENDPOINT') or _get_config('endpoint')
    if not endpoint:
        return None
    return endpoint.rstrip('/')


def get_endpoint_url() -> str:
    """Returns the RustFS S3 endpoint.

    Raises:
        ValueError: if the endpoint is not configured.
    """
    endpoint = get_endpoint()
    if not endpoint:
        raise ValueError(
            'RustFS endpoint is not configured. Set it in ~/.sky/config.yaml'
            ' under `rustfs.endpoint` or via the RUSTFS_ENDPOINT environment'
            ' variable.')
    return endpoint


def get_access_key() -> Optional[str]:
    """Returns the RustFS access key."""
    return os.environ.get('RUSTFS_ACCESS_KEY') or _get_config('access_key')


def get_secret_key() -> Optional[str]:
    """Returns the RustFS secret key."""
    return os.environ.get('RUSTFS_SECRET_KEY') or _get_config('secret_key')


def get_region() -> str:
    """Returns the region used for SigV4 signing.

    RustFS is region-agnostic and accepts the default region, but the
    region has to match what the server expects, so it is configurable.
    """
    return (os.environ.get('RUSTFS_REGION') or _get_config('region') or
            DEFAULT_REGION)


def _credentials_file_content(access_key: str, secret_key: str) -> str:
    """Returns the content of the AWS-style RustFS credentials file."""
    return (f'[{RUSTFS_PROFILE_NAME}]\n'
            f'aws_access_key_id = {access_key}\n'
            f'aws_secret_access_key = {secret_key}\n')


def ensure_credentials_file() -> Optional[str]:
    """Writes the RustFS credentials to an AWS-style credentials file.

    The file is mounted to the remote nodes, where the mount helpers
    (goofys / rclone / aws cli) read the credentials from it.

    Returns:
        The path of the credentials file, or None if the credentials are not
        configured or the file could not be written.
    """
    access_key = get_access_key()
    secret_key = get_secret_key()
    if not access_key or not secret_key:
        return None
    path = os.path.expanduser(RUSTFS_CREDENTIALS_PATH)
    content = _credentials_file_content(access_key, secret_key)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Do not rewrite the file when it is already up to date: the file is
        # read by concurrent launches, and an unnecessary rewrite would
        # invalidate the file mounts of the clusters that are already up.
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                if f.read() == content:
                    return path
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
        os.chmod(path, 0o600)
    except OSError as e:
        logger.warning('Failed to write the RustFS credentials file '
                       f'{path}: {e}')
        return None
    return path


def session():
    """Creates a boto3 session for RustFS."""
    access_key = get_access_key()
    secret_key = get_secret_key()
    if not access_key or not secret_key:
        with ux_utils.print_exception_no_traceback():
            raise ValueError(
                'RustFS credentials not found. Configure them in '
                '~/.sky/config.yaml (rustfs.access_key and rustfs.secret_key)'
                ' or via the RUSTFS_ACCESS_KEY and RUSTFS_SECRET_KEY '
                'environment variables.')
    # Creating the session object is not thread-safe for boto3, so we add a
    # reentrant lock to synchronize the session creation.
    with _session_creation_lock:
        return boto3.session.Session(aws_access_key_id=access_key,
                                     aws_secret_access_key=secret_key,
                                     region_name=get_region())


@common.load_lazy_modules(_LAZY_MODULES)
def _botocore_config():
    """Returns a botocore Config suitable for RustFS.

    RustFS serves path-style addressing by default: the bucket is part of
    the URL path rather than the hostname, so requests must not be rewritten
    into the virtual-hosted style.
    """
    return botocore.config.Config(s3={'addressing_style': 'path'})


@common.load_lazy_modules(_LAZY_MODULES)
def _health_check_config():
    """Returns a botocore Config for the reachability probe.

    The probe runs on every launch (through ``sky check``), so it must fail
    fast instead of blocking on the default 60 second timeout when the
    server is unreachable.
    """
    return botocore.config.Config(s3={'addressing_style': 'path'},
                                  connect_timeout=5,
                                  read_timeout=10,
                                  retries={'max_attempts': 1})


def client(service_name: str, config: Optional[Any] = None):
    """Creates a RustFS client of a certain service (e.g. 's3')."""
    session_ = session()
    if config is None:
        config = _botocore_config()
    return session_.client(service_name,
                           endpoint_url=get_endpoint_url(),
                           region_name=get_region(),
                           config=config)


def resource(resource_name: str, **kwargs):
    """Creates a RustFS resource (e.g. 's3')."""
    session_ = session()
    return session_.resource(resource_name,
                             endpoint_url=get_endpoint_url(),
                             region_name=get_region(),
                             config=_botocore_config(),
                             **kwargs)


@common.load_lazy_modules(_LAZY_MODULES)
def botocore_exceptions():
    """Returns the botocore exceptions module."""
    # pylint: disable=import-outside-toplevel
    from botocore import exceptions as boto_exceptions
    return boto_exceptions


def check_credentials(
        cloud_capability: cloud.CloudCapability) -> Tuple[bool, Optional[str]]:
    if cloud_capability == cloud.CloudCapability.STORAGE:
        return check_storage_credentials()
    else:
        raise exceptions.NotSupportedError(
            f'{NAME} does not support {cloud_capability}.')


def check_storage_credentials() -> Tuple[bool, Optional[str]]:
    """Checks whether RustFS is configured and reachable.

    Returns:
        A tuple of a boolean value and a hint message where the bool is
        True when RustFS is configured and usable. It is False otherwise,
        which would hint with a string on how to configure it.
    """
    endpoint = get_endpoint()
    if not endpoint:
        return (False, 'RustFS endpoint is not set. Set it in '
                '~/.sky/config.yaml under `rustfs.endpoint` or via the '
                'RUSTFS_ENDPOINT environment variable.')
    if not get_access_key() or not get_secret_key():
        return (False, 'RustFS credentials are not set. Set them in '
                '~/.sky/config.yaml under `rustfs.access_key` and '
                '`rustfs.secret_key`, or via the RUSTFS_ACCESS_KEY and '
                'RUSTFS_SECRET_KEY environment variables.')

    # Materialize the credentials file: it is mounted to the remote nodes,
    # where the mount helpers authenticate against RustFS with it.
    if ensure_credentials_file() is None:
        return (False, 'Failed to write the RustFS credentials file to '
                f'{RUSTFS_CREDENTIALS_PATH}. The mount helpers on the remote '
                'node need it to authenticate against RustFS.')

    try:
        client('s3', config=_health_check_config()).list_buckets()
    except botocore_exceptions().ClientError as e:
        error_code = e.response.get('Error', {}).get('Code', '')
        if error_code in ('AccessDenied', '403'):
            # The endpoint is reachable, but the credentials are not allowed
            # to list the buckets. That does not prevent mounting a bucket
            # the credentials do have access to.
            return (True, None)
        return (False, f'Failed to access RustFS at {endpoint}: {e}')
    except botocore_exceptions().BotoCoreError as e:
        return (False, f'Failed to connect to RustFS at {endpoint}: {e}')
    return (True, None)


def get_credential_file_mounts() -> Dict[str, str]:
    """Returns the credential file mounts for RustFS.

    Returns:
        Dict[str, str]: A dictionary mapping source paths to destination paths
        for credential files.
    """
    return {RUSTFS_CREDENTIALS_PATH: RUSTFS_CREDENTIALS_PATH}
