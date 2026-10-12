"""Publication guard: inspect source files and nested fixtures without printing secrets.

This complements human review; arbitrary host names in prose need manual review.
Only the public localhost test key and two exact invalid-URL test cases are exempt.
"""
import hashlib
import io
import ipaddress
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DOC_NETS = tuple(ipaddress.ip_network(n) for n in (
    '192.0.2.0/24', '198.51.100.0/24', '203.0.113.0/24'))
LAN_NETS = tuple(ipaddress.ip_network((int(ipaddress.IPv4Address(bytes(address))), prefix))
                 for address, prefix in (((10, 0, 0, 0), 8), ((172, 16, 0, 0), 12), ((192, 168, 0, 0), 16)))
PUBLIC_HOSTS = frozenset((
    'github.com', 'api.github.com', 'raw.githubusercontent.com', 'codeload.github.com',
    'yandex.ru', 'api.ipify.org', 'ipv4.icanhazip.com', 'checkip.amazonaws.com',
    'docker.n8n.io', 'docs.n8n.io', 'dl-cdn.alpinelinux.org', 'grafana.com',
    'www.w3.org', 'download.docker.com', 'docker.io', 'api.telegram.org',
    'spd-rudp.hostkey.ru', 'mskst.st.mtsws.net', 'msk.speed.ruweb.net'))
TOKEN_PATTERNS = tuple(re.compile(pattern) for pattern in (
    r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,})\b',
    r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b', r'\b\d{7,12}:[A-Za-z0-9_-]{30,}\b',
    r'\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b',
    r'-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----'))
IP = re.compile(r'(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])')
URL = re.compile(r'https?://[^\s\"\'<>`\\]+')
USER_PATH = re.compile(r'(?i)(?:[A-Z]:[/\\](?:Users|Projects)[/\\]|/ho' r'me/[^/\s]+/)')
OLD_LINK = re.compile('https' + r'://github\.com/[^/]+/[^/]+/(?:issues|pull|actions/runs)/\d+')
EXEMPT_URLS = {
    'tests/test_audit.py': 'https://' + 'secret:password@example.test',
    'tests/test_installer.py': 'https://' + 'user:pass@example.com',
}
# Filled with the hash of a public, test-only localhost TLS key, never a deployment key.
PUBLIC_KEY_SHA256 = '8c0bf085f340d5c4ef8bea8a00fa84a90cadbd0b72304a0a9ad3fe12cb5eb66c'
IGNORE_DIRS = frozenset(('.git', '__pycache__', '.venv', 'dist', '.docker-client', '.stolas', 'data'))


def safe_host(host, test):
    if host in PUBLIC_HOSTS or host in ('localhost', 'host.docker.internal'):
        return True
    if host and any(host == suffix or host.endswith('.' + suffix)
                    for suffix in ('example.test', 'example.net', 'example.com', 'invalid', 'test', 'example')):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if address.is_loopback or address.is_unspecified:
        return True
    if address.version == 6:
        return address in ipaddress.ip_network('2001:db8::/32')
    return any(address in n for n in DOC_NETS) or test and any(address in n for n in LAN_NETS)


def inspect(data, name, depth=0, total=None):
    """Return locations/categories only; never echo matched credentials or addresses."""
    from urllib.parse import urlsplit
    total = [0] if total is None else total
    total[0] += len(data)
    if depth > 6 or len(data) > 32 * 1024 * 1024 or total[0] > 256 * 1024 * 1024:
        return [(name, 'scan limit exceeded')]
    leaf = name.rsplit('!', 1)[-1]
    parts = PurePosixPath(leaf).parts
    if depth and parts and parts[0].startswith('stolas-'):
        leaf = '/'.join(parts[1:])
        parts = PurePosixPath(leaf).parts
    if ('..' in parts or PurePosixPath(leaf).is_absolute() or
            any(p in ('.git', '.aws', '.ssh', 'data', '.stolas') for p in parts) or
            leaf in ('.env', 'config/local.json', 'n8n/local.json', 'n8n/settings.json', 'n8n/install-state.json') or
            leaf.endswith(('.sqlite', '.sqlite3', '.db', '.log', '.zip')) and not leaf.startswith('tests/fixtures/')):
        return [(name, 'private runtime file or unsafe path')]
    result = []
    if data.startswith(b'\x1f\x8b') or leaf.endswith(('.tar', '.tar.gz', '.tgz', '.zip')):
        try:
            if leaf.endswith('.zip'):
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    for member in archive.infolist():
                        if member.is_dir():
                            continue
                        if member.file_size > 32 * 1024 * 1024:
                            result.append((name, 'oversized archive member'))
                        else:
                            result.extend(inspect(archive.read(member), name + '!' + member.filename, depth + 1, total))
            else:
                with tarfile.open(fileobj=io.BytesIO(data), mode='r:*') as archive:
                    for member in archive:
                        if member.isdir():
                            continue
                        if not member.isfile() or member.size > 32 * 1024 * 1024:
                            result.append((name, 'unsupported archive member'))
                        else:
                            result.extend(inspect(archive.extractfile(member).read(), name + '!' + member.name, depth + 1, total))
        except (tarfile.TarError, zipfile.BadZipFile, OSError, EOFError):
            result.append((name, 'unreadable archive'))
        return result
    test = leaf.startswith('tests/')
    text = data.decode('utf-8', errors='replace')
    exempt_key = (leaf == 'tests/fixtures/localhost-test-key.pem' and
                  hashlib.sha256(data).hexdigest() == PUBLIC_KEY_SHA256)
    for number, line in enumerate(text.splitlines(), 1):
        location = name + ':' + str(number)
        if any(p.search(line) for p in TOKEN_PATTERNS) and not exempt_key:
            result.append((location, 'credential pattern'))
        if USER_PATH.search(line):
            result.append((location, 'absolute user path'))
        if OLD_LINK.search(line):
            result.append((location, 'repository history link: review before publication'))
        for match in IP.finditer(line):
            try:
                address = ipaddress.IPv4Address(match.group())
            except ValueError:
                continue  # Dependency versions can resemble addresses.
            # Deliberate public-address rejection case in the topology unit test.
            public_negative = leaf == 'tests/test_audit.py' and address.packed == bytes((8, 8, 8, 8))
            if not safe_host(str(address), test) and not public_negative:
                result.append((location, 'address outside documentation/test ranges'))
        for match in URL.finditer(line):
            value = match.group().rstrip('),;.')
            if any(c in value for c in '{}[]$'):
                continue  # Runtime interpolation; reviewed in source.
            try:
                parsed = urlsplit(value)
                host = parsed.hostname
            except ValueError:
                result.append((location, 'unparseable URL'))
                continue
            if parsed.username is not None and EXEMPT_URLS.get(leaf) != value:
                result.append((location, 'URL credentials'))
            public_negative = leaf == 'tests/test_audit.py' and host == str(ipaddress.IPv4Address(bytes((8, 8, 8, 8))))
            if host and not safe_host(host, test) and not public_negative:
                result.append((location, 'unreviewed URL host'))
    return result


def source_files(root):
    try:
        top = subprocess.check_output(['git', '-C', str(root), 'rev-parse', '--show-toplevel'], stderr=subprocess.DEVNULL).decode().strip()
        if Path(top).resolve() == root.resolve():
            names = subprocess.check_output(['git', '-C', str(root), 'ls-files', '--cached', '--others', '--exclude-standard', '-z']).decode().split('\0')
            return [root / name for name in names if name]
    except (OSError, subprocess.CalledProcessError):
        pass
    return [p for p in root.rglob('*') if p.is_file() and not IGNORE_DIRS.intersection(p.relative_to(root).parts)]


def main():
    findings = []
    files = source_files(ROOT)
    for path in files:
        if path.is_symlink():
            findings.append((path.relative_to(ROOT).as_posix(), 'source symlink'))
        else:
            findings.extend(inspect(path.read_bytes(), path.relative_to(ROOT).as_posix()))
    for location, category in findings:
        print(location + ': ' + category)
    print(f'Publication guard: {len(files)} files, {len(findings)} findings')
    return bool(findings)


if __name__ == '__main__':
    sys.exit(main())
