"""Maintainer tool: resolve and hash the Alpine dependency closure for both architectures."""
import hashlib
import io
import json
import re
import tarfile
import urllib.request
from pathlib import Path


def fetch(url):
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read()


def main():
    lock = {"base": "alpine:3.23.0@sha256:51183f2cfa6320055da30872f211093f9ff1d3cf06f39a0bdb212314c5dc7375", "architectures": {}}
    for arch in ("x86_64", "aarch64"):
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
        Path("config").mkdir(exist_ok=True)
        Path("config/apk-" + arch + ".lock").write_text("".join(p["sha256"] + " " + p["url"] + "\n" for p in items), encoding="utf-8", newline="\n")
        print(arch, len(items), "packages", flush=True)
    Path("config/apk.lock.json").write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
