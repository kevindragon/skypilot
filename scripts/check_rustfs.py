#!/usr/bin/env python3
"""Self-check for the RustFS storage integration with SkyPilot.

Run this on the machine that talks to RustFS (the SkyPilot API server for a
Kubernetes deployment, or the machine running ``sky``) to find out why a
``rustfs://`` bucket is not usable.

It checks, in order:

  1. configuration -- endpoint, region and credentials, and where they come
     from (environment variables or ``~/.sky/config.yaml``);
  2. the credentials file that SkyPilot mounts onto the cluster nodes;
  3. HTTP reachability of the endpoint;
  4. the S3 API (list buckets);
  5. access to one bucket, and optionally a put/get/delete round trip;
  6. the FUSE prerequisites of MOUNT mode;
  7. whether ``sky check`` reports RustFS as enabled.

Examples::

    python scripts/check_rustfs.py
    python scripts/check_rustfs.py --bucket mybucket --write-test
    python scripts/check_rustfs.py --endpoint http://10.0.0.10:7112
    python scripts/check_rustfs.py --skip-sky-check

Exit code is 0 when no check fails; warnings do not fail the run.
"""

# pylint: disable=import-outside-toplevel
# sky is imported inside the functions on purpose: the script has to print a
# friendly message when SkyPilot is not importable, and --endpoint must set the
# environment variable before sky.adaptors.rustfs reads it.

import argparse
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

PASS = 'PASS'
WARN = 'WARN'
FAIL = 'FAIL'

_SKY_CHECK_TIMEOUT_SECONDS = 180
_PROBE_TIMEOUT_SECONDS = 5


def _mask(secret):
    """Masks a credential, keeping a short prefix so it can be recognized."""
    if not secret:
        return '<unset>'
    if len(secret) <= 6:
        return '*' * len(secret)
    return f'{secret[:3]}***{secret[-2:]}'


class Results:
    """Collects check results and prints a final summary."""

    def __init__(self):
        self.rows = []

    def add(self, status, name, detail='', hint=''):
        self.rows.append((status, name, detail, hint))
        line = f'[{status}] {name}'
        if detail:
            line += f': {detail}'
        print(line)
        if hint:
            for hint_line in hint.strip().splitlines():
                print(f'       hint: {hint_line.strip()}')

    def count(self, status):
        return sum(1 for row in self.rows if row[0] == status)

    def summary(self):
        failures = self.count(FAIL)
        warnings = self.count(WARN)
        passed = self.count(PASS)
        print()
        print(f'Summary: {passed} passed, {warnings} warning(s), '
              f'{failures} failed.')
        if failures:
            print('\nFix the FAIL items above; each one carries a hint.')
            return 1
        if warnings:
            print('\nNo hard failures. Review the warnings before relying on '
                  'the setup.')
            return 0
        print('\nAll checks passed.')
        return 0


def check_configuration(results):
    """Checks that endpoint, region and credentials are configured."""
    from sky.adaptors import rustfs

    endpoint = rustfs.get_endpoint()
    if endpoint:
        source = ('environment' if os.environ.get('RUSTFS_ENDPOINT') else
                  '~/.sky/config.yaml')
        results.add(PASS, 'endpoint configured', f'{endpoint} (from {source})')
    else:
        results.add(
            FAIL, 'endpoint configured', '<unset>',
            'Set rustfs.endpoint in ~/.sky/config.yaml or export '
            'RUSTFS_ENDPOINT, e.g. http://10.0.0.10:7112.')

    access_key = rustfs.get_access_key()
    secret_key = rustfs.get_secret_key()
    if access_key and secret_key:
        results.add(
            PASS, 'credentials configured', f'access_key={_mask(access_key)} '
            f'secret_key={_mask(secret_key)}')
    else:
        missing = []
        if not access_key:
            missing.append('access_key')
        if not secret_key:
            missing.append('secret_key')
        results.add(
            FAIL, 'credentials configured', f'missing {", ".join(missing)}',
            'Set rustfs.access_key and rustfs.secret_key in '
            '~/.sky/config.yaml (the RUSTFS_ACCESS_KEY / RUSTFS_SECRET_KEY '
            'environment variables also work).')

    results.add(PASS, 'region', rustfs.get_region())
    return bool(endpoint and access_key and secret_key)


def check_credentials_file(results):
    """Checks the AWS-style credentials file that nodes mount."""
    from sky.adaptors import rustfs

    path = os.path.expanduser(rustfs.RUSTFS_CREDENTIALS_PATH)
    written = rustfs.ensure_credentials_file()
    if written is None:
        results.add(
            FAIL, 'credentials file', f'cannot write {path}',
            'The file is mounted to the cluster nodes, where goofys, rclone '
            'and the aws cli read the credentials from it. On a read-only '
            'container filesystem, mount a writable volume at ~/.rustfs.')
        return False

    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    profile = f'[{rustfs.RUSTFS_PROFILE_NAME}]'
    if profile not in content:
        results.add(FAIL, 'credentials file',
                    f'{path} has no {profile} profile',
                    'Delete the file and rerun this script to regenerate it.')
        return False

    access_key = rustfs.get_access_key()
    if access_key and f'aws_access_key_id = {access_key}' not in content:
        results.add(WARN, 'credentials file',
                    f'{path} does not match the configured access key',
                    'Delete the file and rerun this script to regenerate it.')
        return True

    results.add(PASS, 'credentials file', path)
    return True


def check_reachability(results, endpoint):
    """Probes the endpoint over HTTP."""
    try:
        with urllib.request.urlopen(endpoint,
                                    timeout=_PROBE_TIMEOUT_SECONDS) as resp:
            results.add(PASS, 'endpoint reachable', f'HTTP {resp.status}')
            return True
    except urllib.error.HTTPError as e:
        # Any HTTP response means something is listening and speaking HTTP.
        results.add(PASS, 'endpoint reachable', f'HTTP {e.code}')
        return True
    except Exception as e:  # pylint: disable=broad-except
        results.add(
            FAIL, 'endpoint reachable', f'{type(e).__name__}: {e}',
            'Check the address and port, that RustFS is running, and that '
            'no firewall, NetworkPolicy or security group blocks it from '
            'this machine and from the compute nodes.')
        return False


def check_s3_api(results):
    """Checks that the S3 API answers with the configured credentials."""
    from sky.adaptors import rustfs

    try:
        buckets = rustfs.client('s3').list_buckets()
    except rustfs.botocore_exceptions().ClientError as e:
        code = e.response.get('Error', {}).get('Code', '')
        if code in ('AccessDenied', '403'):
            results.add(
                WARN, 'S3 API', 'credentials cannot list buckets',
                'Mounting a bucket the credentials do have access to still '
                'works; pass --bucket to check that bucket specifically.')
            return True
        results.add(FAIL, 'S3 API', str(e),
                    'Verify the access key / secret key match the server.')
        return False
    except rustfs.botocore_exceptions().BotoCoreError as e:
        results.add(
            FAIL, 'S3 API', str(e),
            'The endpoint answered HTTP but not the S3 API; confirm '
            'the port is the RustFS S3 API port (7112 by default).')
        return False

    names = [b['Name'] for b in buckets.get('Buckets', [])]
    shown = ', '.join(names[:5]) + (' ...' if len(names) > 5 else '')
    results.add(PASS, 'S3 API', f'{len(names)} bucket(s): {shown}')
    return True


def check_bucket(results, bucket, write_test):
    """Checks access to one bucket, optionally with a write round trip."""
    from sky.adaptors import rustfs

    client = rustfs.client('s3')
    try:
        client.head_bucket(Bucket=bucket)
    except rustfs.botocore_exceptions().ClientError as e:
        code = e.response.get('Error', {}).get('Code', '')
        results.add(
            FAIL, f'bucket {bucket}', f'{code or e}', 'Create it first, e.g. '
            f'`aws --endpoint-url {rustfs.get_endpoint()} s3 mb '
            f's3://{bucket}`, and check the credentials can read it.')
        return False
    results.add(PASS, f'bucket {bucket}', 'exists and is readable')

    try:
        listed = client.list_objects_v2(Bucket=bucket, MaxKeys=5)
    except rustfs.botocore_exceptions().ClientError as e:
        results.add(WARN, f'bucket {bucket} listing', str(e))
    else:
        count = listed.get('KeyCount', 0)
        results.add(PASS, f'bucket {bucket} listing',
                    f'{count} object(s) in the first page')

    if not write_test:
        return True

    key = '.skypilot-rustfs-selfcheck'
    payload = b'skypilot rustfs self-check\n'
    try:
        client.put_object(Bucket=bucket, Key=key, Body=payload)
        fetched = client.get_object(Bucket=bucket, Key=key)['Body'].read()
        client.delete_object(Bucket=bucket, Key=key)
    except rustfs.botocore_exceptions().ClientError as e:
        results.add(
            FAIL, f'bucket {bucket} write test', str(e),
            'MOUNT mode and COPY mode both need write access unless '
            'the mount is configured read-only.')
        return False

    if fetched != payload:
        results.add(FAIL, f'bucket {bucket} write test',
                    'content mismatch after round trip')
        return False
    results.add(PASS, f'bucket {bucket} write test', 'put/get/delete ok')
    return True


def check_fuse(results):
    """Checks the FUSE prerequisites of MOUNT mode."""
    if not os.path.exists('/dev/fuse'):
        results.add(
            WARN, 'FUSE device', '/dev/fuse not found',
            'MOUNT and MOUNT_CACHED need FUSE. On Kubernetes, deploy the '
            'fusermount-server DaemonSet (addons/fuse-proxy). If you only '
            'use COPY mode, this is harmless.')
        return False

    fusermount = shutil.which('fusermount') or shutil.which('fusermount3')
    if fusermount:
        results.add(PASS, 'FUSE device', f'/dev/fuse, {fusermount}')
    else:
        results.add(WARN, 'FUSE device',
                    '/dev/fuse present but no fusermount binary',
                    'Install fuse3 (e.g. apt-get install -y fuse3).')
    return True


def check_sky_check(results):
    """Checks that `sky check` reports RustFS as enabled."""
    sky = shutil.which('sky')
    if not sky:
        results.add(WARN, 'sky check', 'sky CLI not on PATH',
                    'Run `sky check` manually once the CLI is available.')
        return False
    try:
        proc = subprocess.run([sky, 'check'],
                              capture_output=True,
                              text=True,
                              timeout=_SKY_CHECK_TIMEOUT_SECONDS,
                              check=False)
    except subprocess.TimeoutExpired:
        results.add(WARN, 'sky check',
                    f'timed out after {_SKY_CHECK_TIMEOUT_SECONDS}s')
        return False

    output = f'{proc.stdout}\n{proc.stderr}'
    if 'RustFS' in output:
        results.add(PASS, 'sky check', 'RustFS reported')
        return True
    results.add(
        WARN, 'sky check', 'RustFS not mentioned in the output',
        'Run `sky check` and read the RustFS row; it prints the reason when '
        'storage access is disabled. If you run SkyPilot on Kubernetes, the '
        'check runs on the API server, which needs the same rustfs config.')
    return False


def main():
    parser = argparse.ArgumentParser(
        description='Self-check the RustFS storage integration with SkyPilot.')
    parser.add_argument('--bucket', help='Bucket to check (e.g. test-sky).')
    parser.add_argument('--write-test',
                        action='store_true',
                        help='Also put/get/delete a small object in --bucket.')
    parser.add_argument('--endpoint',
                        help='Override the endpoint for this run only.')
    parser.add_argument('--skip-sky-check',
                        action='store_true',
                        help='Do not run `sky check` (it can be slow).')
    args = parser.parse_args()

    if args.write_test and not args.bucket:
        parser.error('--write-test requires --bucket')
    if args.endpoint:
        os.environ['RUSTFS_ENDPOINT'] = args.endpoint

    try:
        from sky.adaptors import rustfs
    except ImportError as e:
        print(f'[{FAIL}] import sky: {e}')
        print('\nRun this script with the Python environment that has '
              'SkyPilot installed, or `pip install -e .` in the repository.')
        return 1

    results = Results()
    configured = check_configuration(results)
    if not configured:
        return results.summary()

    check_credentials_file(results)

    if not check_reachability(results, rustfs.get_endpoint()):
        return results.summary()

    if check_s3_api(results) and args.bucket:
        check_bucket(results, args.bucket, args.write_test)

    check_fuse(results)
    if not args.skip_sky_check:
        check_sky_check(results)

    return results.summary()


if __name__ == '__main__':
    sys.exit(main())
