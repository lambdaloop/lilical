#!/usr/bin/env python3
"""Replace _PLACEHOLDER_ URL segments in python3-modules.yml with real PyPI paths."""
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

MODULES_FILE = Path(__file__).parent.parent / "python3-modules.yml"
PLACEHOLDER = "_PLACEHOLDER_"
PYPI_JSON = "https://pypi.org/pypi/{name}/{version}/json"

# Wheel filename: {name}-{version}(-{build})?-{pytag}-{abitag}-{platformtag}.whl
# or sdist: {name}-{version}.tar.gz
_WHL_RE = re.compile(r"^(?P<name>[A-Za-z0-9_.-]+?)-(?P<version>\d[^-]*)")


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def pypi_url_for(filename: str, sha256: str) -> str | None:
    m = _WHL_RE.match(filename)
    if not m:
        return None
    pkg_name = normalize(m.group("name"))
    version = m.group("version")
    api_url = PYPI_JSON.format(name=pkg_name, version=version)
    try:
        with urllib.request.urlopen(api_url, timeout=10) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} for {api_url}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  Error fetching {api_url}: {e}", file=sys.stderr)
        return None

    for entry in data.get("urls", []):
        if entry["filename"] == filename:
            if entry["digests"]["sha256"] != sha256:
                print(f"  WARNING sha256 mismatch for {filename}", file=sys.stderr)
            return entry["url"]
    # Try all release assets (some may be in data['releases'][version])
    for entry in data.get("releases", {}).get(version, []):
        if entry["filename"] == filename:
            return entry["url"]
    print(f"  NOT FOUND on PyPI: {filename}", file=sys.stderr)
    return None


def main() -> None:
    text = MODULES_FILE.read_text()

    # Find all placeholder lines and replace them
    pattern = re.compile(
        r"(url:\s+https://files\.pythonhosted\.org/packages/)"
        + re.escape(PLACEHOLDER)
        + r"(/([^\s]+))"
    )

    failures = []
    counts = {"fixed": 0, "failed": 0}

    def replace(m: re.Match) -> str:
        prefix = m.group(1)
        suffix = m.group(2)
        filename = m.group(3)
        # sha256 is on the next line — we'll look it up separately
        return prefix + PLACEHOLDER + suffix  # return unchanged; we'll fix below

    # Collect all (filename, sha256) pairs
    entries = re.findall(
        r"url:\s+https://files\.pythonhosted\.org/packages/_PLACEHOLDER_/([^\s]+)\s+"
        r"sha256:\s+([a-f0-9]+)",
        text,
    )

    if not entries:
        print("No placeholders found — nothing to do.")
        return

    print(f"Found {len(entries)} placeholder entries to resolve...")

    url_map: dict[str, str] = {}
    for i, (filename, sha256) in enumerate(entries, 1):
        sys.stdout.write(f"\r  [{i}/{len(entries)}] {filename[:60]:<60}")
        sys.stdout.flush()
        real_url = pypi_url_for(filename, sha256)
        if real_url:
            url_map[filename] = real_url
            counts["fixed"] += 1
        else:
            failures.append(filename)
            counts["failed"] += 1
        time.sleep(0.05)  # be polite to PyPI

    print()

    # Now do the replacements
    def sub(m: re.Match) -> str:
        filename = m.group(3)
        if filename in url_map:
            real_url = url_map[filename]
            # Extract the path portion after /packages/
            path_part = real_url.split("/packages/", 1)[1]
            return m.group(1) + path_part[: path_part.rfind("/")] + m.group(2)
        return m.group(0)

    new_text = pattern.sub(sub, text)
    MODULES_FILE.write_text(new_text)

    print(f"\nDone: {counts['fixed']} fixed, {counts['failed']} failed.")
    if failures:
        print("Failed packages:")
        for f in failures:
            print(f"  {f}")
        sys.exit(1)


if __name__ == "__main__":
    main()
