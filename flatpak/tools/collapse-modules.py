#!/usr/bin/env python3
"""
Collapse a multi-module flatpak-pip-generator YAML into a single flat module
that installs all wheels at once with `pip3 install *.whl`.

The per-package format includes source tarballs alongside wheels; when pip
installs by name it can pick the tarball and try building from source (which
fails for packages with Rust build backends like dbus_fast and cffi 2.x).
The flat *.whl install avoids this entirely.

Usage: python3 collapse-modules.py <input.yml> <output.yml>
"""
import sys
import yaml

input_path, output_path = sys.argv[1], sys.argv[2]

with open(input_path) as f:
    data = yaml.safe_load(f)

wheels: list[dict] = []
seen: set[str] = set()

def collect(obj: object) -> None:
    if isinstance(obj, dict):
        url = obj.get("url", "")
        if obj.get("type") == "file" and url.endswith(".whl") and url not in seen:
            seen.add(url)
            wheels.append({"type": "file", "url": url, "sha256": obj["sha256"]})
        for v in obj.values():
            collect(v)
    elif isinstance(obj, list):
        for item in obj:
            collect(item)

collect(data)

out = {
    "name": "python3-deps",
    "buildsystem": "simple",
    "build-commands": [
        "pip3 install --verbose --no-index --ignore-installed"
        ' --find-links="file://${PWD}" --prefix=${FLATPAK_DEST} *.whl'
    ],
    "sources": wheels,
}

with open(output_path, "w") as f:
    yaml.dump(out, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

print(f"Collapsed {len(wheels)} wheels into {output_path}")
