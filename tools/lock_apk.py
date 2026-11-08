"""Resolve reviewed APK pins, or verify their availability without changing them."""
import argparse
import hashlib
import http.client
import io
import json
import re
import shutil
import ssl
import tarfile
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARCHITECTURES = ("x86_64", "aarch64")
UPDATE_DIRECTORY = ".apk-lock-update"


def validate_url(url):
    # Locks contain public artifact URLs, never credentials or signed query strings.
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme == "https" and parsed.hostname and
                 parsed.username is None and parsed.password is None and
                 not parsed.query and not parsed.fragment and
                 not any(c.isspace() or ord(c) < 32 for c in url))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("APK URL must use HTTPS without credentials, query or fragment")
    return parsed


def fetch(url):
    validate_url(url)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            retry = error.code in (408, 429, 500, 502, 503, 504)
            reason = f"HTTP {error.code}"
            error.close()
        except (urllib.error.URLError, OSError, http.client.HTTPException) as error:
            cause = error.reason if isinstance(error, urllib.error.URLError) else error
            retry = isinstance(cause, (OSError, http.client.HTTPException)) and not isinstance(cause, ssl.SSLError)
            reason = type(cause).__name__
        if not retry or attempt == 2:
            # Do not echo response bodies, redirect URLs or exception messages.
            raise ValueError(f"APK download failed: {reason}: {url} (attempts: {attempt + 1})") from None
        time.sleep(2 ** attempt)


def lock_text(packages):
    return "".join(p["sha256"] + " " + p["url"] + "\n" for p in packages)


def check_lock(root=ROOT):
    """GET and hash every pinned payload; never resolve an index or rewrite pins."""
    if (root / "config" / UPDATE_DIRECTORY).exists():
        raise ValueError("APK lock update is in progress or incomplete; inspect config/.apk-lock-update")
    lock = json.loads((root / "config/apk.lock.json").read_text())
    if set(lock["architectures"]) != set(ARCHITECTURES):
        raise ValueError("APK lock must contain x86_64 and aarch64")
    if "FROM " + lock["base"] not in (root / "Dockerfile").read_text().splitlines():
        raise ValueError("APK lock base differs from Dockerfile")
    for arch, packages in lock["architectures"].items():
        if not packages:
            raise ValueError(f"Empty APK lock: {arch}")
        if (root / f"config/apk-{arch}.lock").read_text() != lock_text(packages):
            raise ValueError(f"APK lock files disagree: {arch}")
        names = set()
        for p in packages:
            if (not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9+_.-]*", p["name"]) or
                    not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9+_.~-]*", p["version"]) or
                    p["name"] in names):
                raise ValueError(f"Invalid or duplicate APK package: {arch}")
            names.add(p["name"])
            if not re.fullmatch(r"[0-9a-f]{64}", p["sha256"]):
                raise ValueError(f"Invalid APK SHA-256: {arch}/{p['name']}")
            parsed = validate_url(p["url"])
            if not parsed.path.endswith(f"/{arch}/{p['name']}-{p['version']}.apk"):
                raise ValueError(f"APK URL does not match package version/architecture: {arch}/{p['name']}")
    count = 0
    for arch, packages in lock["architectures"].items():
        for p in packages:
            if hashlib.sha256(fetch(p["url"])).hexdigest() != p["sha256"]:
                raise ValueError(f"APK SHA-256 mismatch: {arch}/{p['name']}: {p['url']}")
            count += 1
    return count


def resolve_lock():
    lock = {"base": "alpine:3.23.0@sha256:51183f2cfa6320055da30872f211093f9ff1d3cf06f39a0bdb212314c5dc7375", "architectures": {}}
    for arch in ARCHITECTURES:
        base = "https://dl-cdn.alpinelinux.org/alpine/v3.23/main/" + arch
        archive = tarfile.open(fileobj=io.BytesIO(fetch(base + "/APKINDEX.tar.gz")), mode="r:gz")
        packages, providers = {}, {}
        for block in archive.extractfile("APKINDEX").read().decode().split("\n\n"):
            fields = dict(line.split(":", 1) for line in block.splitlines() if ":" in line)
            if "P" not in fields:
                continue
            packages[fields["P"]] = fields
            for item in fields.get("p", "").split():
                providers[re.split(r"[=<>~]", item)[0]] = fields["P"]
        # Preserve Alpine's shell provider instead of selecting an arbitrary /bin/sh.
        providers["/bin/sh"] = "busybox-binsh"
        providers["cmd:sh"] = "busybox-binsh"
        # Lock the base package set too: upgrades of musl/busybox must not conflict
        # with exact-version dependencies of the original base-image utilities.
        wanted, resolved = ["python3", "iperf3", "iproute2-minimal", "ca-certificates",
                            "ca-certificates-bundle", "alpine-baselayout", "alpine-keys",
                            "apk-tools", "busybox-binsh", "musl-utils", "scanelf", "ssl_client"], set()
        while wanted:
            dep = re.split(r"[=<>~]", wanted.pop())[0]
            if dep.startswith("!"):
                continue
            name = dep if dep in packages else providers[dep]
            if name in resolved:
                continue
            resolved.add(name)
            wanted.extend(packages[name].get("D", "").split())
        items = []
        for name in sorted(resolved):
            p = packages[name]
            filename = name + "-" + p["V"] + ".apk"
            url = base + "/" + filename
            digest = hashlib.sha256(fetch(url)).hexdigest()
            items.append({"name": name, "version": p["V"], "url": url, "sha256": digest})
        lock["architectures"][arch] = items
        print(arch, len(items), "packages", flush=True)
    return lock


def write_lock(lock, root=ROOT):
    # All downloads for both architectures must succeed before any lock is replaced.
    config = root / "config"
    config.mkdir(exist_ok=True)
    files = {f"apk-{arch}.lock": lock_text(packages)
             for arch, packages in lock["architectures"].items()}
    files["apk.lock.json"] = json.dumps(lock, indent=2) + "\n"
    directory = config / UPDATE_DIRECTORY
    try:
        directory.mkdir()
    except FileExistsError:
        raise ValueError("APK lock update is in progress or incomplete; inspect config/.apk-lock-update") from None
    published = []
    originals = set()
    try:
        (directory / "original").mkdir()
        for name, content in files.items():
            if (config / name).exists():
                shutil.copy2(config / name, directory / "original" / name)
                originals.add(name)
            (directory / name).write_text(content, encoding="utf-8", newline="\n")
        for name in files:
            # Record before rename so interruption immediately after rename rolls back too.
            published.append(name)
            (directory / name).replace(config / name)
    except BaseException:
        try:
            for name in reversed(published):
                if name in originals:
                    # Retain the original backup if another restoration fails.
                    shutil.copy2(directory / "original" / name, directory / name)
                    (directory / name).replace(config / name)
                else:
                    (config / name).unlink(missing_ok=True)
        except BaseException:
            raise ValueError("APK lock rollback failed; backups retained in config/.apk-lock-update/original") from None
        shutil.rmtree(directory)
        raise
    shutil.rmtree(directory)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="download and verify existing pins for both architectures; do not update")
    args = parser.parse_args(argv)
    try:
        if args.check:
            print(f"APK lock: {check_lock()} payloads verified")
        else:
            write_lock(resolve_lock())
    except ValueError as error:
        parser.exit(1, str(error) + "\n")
    except OSError as error:
        parser.exit(1, f"APK lock filesystem error ({type(error).__name__}); inspect config/.apk-lock-update\n")


if __name__ == "__main__":
    main()
