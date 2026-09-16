#!/usr/bin/env python3
"""Mirror a published Studio site so another host can serve it.

WHY THIS EXISTS. A published site is a *staged directory*, not a build artefact in
git: `package-website.py` renders it into `$OLYMPUS_SITES_ROOT/<slug>/`, and
`olympus-sites` serves that directory. When the host holding those directories is
retired, the staged copy goes with it — and Studio's sources for the site are on
that same host, so `make site-package` cannot simply rerun somewhere else. The
published names are the only copy left that is reachable without shell access.

So this fetches a published name and writes what it serves. It is a *recovery*
tool, not the publish path: it cannot see a file the site never links to, so it
records a manifest of everything it did fetch and `--verify-base` proves a name
serves those same bytes. Anything unreachable is therefore visible as a missing
entry rather than as a silent omission.

BEFORE YOU TRUST IT, KNOW WHAT IT CANNOT DO. A published name is not necessarily a
static site — on this deployment every name under the studio suffix was a Studio
*application*: a container with an API and a SQLite database. This tool would have
mirrored such an app's HTML and assets faithfully, and the result would have loaded
while every `/api/*` call 404'd and the app's data went unserved — which is exactly
what happened during the `.10` migration, before it was caught and redone with
`app-runtime.py`. A name that answers `/api/health` with JSON (or whose client JS
calls `/api/*`) is an app: move it with `app-runtime.py`, not with this tool.

    # what it would copy (default)
    scripts/mirror-published-site.py --host weight-tracker.studio.olympus.innotel.us \\
        --dest /var/lib/olympus/sites/weight-tracker

    # copy, and write the manifest beside the site directory (not inside it)
    ... --apply

    # after the edge is repointed: prove the *name* serves the recorded bytes.
    # --verify-only, because re-fetching from --host would now read the very thing
    # being checked, and a mirror that verifies itself always passes.
    scripts/mirror-published-site.py --verify-only \\
        --dest /var/lib/olympus/sites/weight-tracker \\
        --verify-base https://weight-tracker.studio.olympus.innotel.us

Exit codes: 0 ok, 1 a fetch or a comparison failed, 2 misconfiguration.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import PurePosixPath

# The manifest is written BESIDE the site's directory, never inside it: the sites
# server serves that directory as the site root, so a manifest in there would
# publish a `/manifest.json` the original site never had — and it would have been
# the one file in the recovery that a `--verify-base` pass could not have caught,
# because it is in the manifest too.
def manifest_path(dest: str) -> str:
    return dest.rstrip("/") + ".manifest.json"

# Enough to follow a static bundle: `<script src>`, `<link href>`, and the
# `url(...)` a stylesheet uses for fonts and images. Deliberately not a browser:
# anything a *link* cannot reach is not in the manifest, which is the caveat the
# docstring opens with rather than something to paper over.
ATTR_RE = re.compile(r"""(?:src|href)\s*=\s*["']([^"']+)["']""", re.I)
CSS_URL_RE = re.compile(r"""url\(\s*['"]?([^'")]+)['"]?\s*\)""", re.I)
SKIP_SCHEMES = ("data:", "mailto:", "tel:", "javascript:", "#")


def fetch(url: str, timeout: int) -> tuple[int, bytes, str]:
    request = urllib.request.Request(url, headers={"user-agent": "olympus-site-mirror/1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(), response.headers.get("content-type", "")
    except urllib.error.HTTPError as error:
        return error.code, error.read(), error.headers.get("content-type", "") if error.headers else ""
    except (urllib.error.URLError, OSError) as error:
        return 0, str(getattr(error, "reason", error)).encode(), ""


def same_origin_refs(body: bytes, content_type: str) -> set[str]:
    """References this document makes that stay on the same origin."""
    text = body.decode("utf-8", "replace")
    refs: set[str] = set()
    if "html" in content_type:
        refs.update(ATTR_RE.findall(text))
    elif "css" in content_type:
        refs.update(CSS_URL_RE.findall(text))
    out = set()
    for ref in refs:
        ref = ref.strip()
        if not ref or ref.startswith(SKIP_SCHEMES):
            continue
        # A protocol-relative or absolute URL points somewhere else; the mirror
        # only claims what this origin serves.
        if ref.startswith("//") or re.match(r"^[a-z][a-z0-9+.-]*:", ref, re.I):
            continue
        out.add(ref)
    return out


def to_origin_path(base_url: str, ref: str) -> str:
    joined = urllib.parse.urljoin(base_url, ref)
    parsed = urllib.parse.urlparse(joined)
    base = urllib.parse.urlparse(base_url)
    if parsed.netloc != base.netloc:
        raise ValueError(f"cross-origin reference: {ref}")
    return parsed.path or "/"


def dest_path(dest: str, origin_path: str) -> str:
    """URL path -> file path, with `/` becoming `index.html`."""
    import os

    clean = PurePosixPath(origin_path.lstrip("/"))
    if origin_path.endswith("/") or str(clean) in (".", ""):
        clean = clean / "index.html"
    if ".." in clean.parts:
        raise ValueError(f"path escapes the destination: {origin_path}")
    return os.path.join(dest, *clean.parts)


def crawl(base_url: str, timeout: int) -> tuple[dict[str, bytes], dict[str, str], list[str]]:
    """Every reachable file, its content type, and the failures."""
    seen: dict[str, bytes] = {}
    types: dict[str, str] = {}
    failures: list[str] = []
    queue = ["/"]

    while queue:
        origin_path = queue.pop()
        if origin_path in seen:
            continue
        url = base_url.rstrip("/") + origin_path
        status, body, content_type = fetch(url, timeout)
        if status != 200:
            failures.append(f"{origin_path} (HTTP {status})")
            continue
        seen[origin_path] = body
        types[origin_path] = content_type.split(";")[0].strip()
        for ref in sorted(same_origin_refs(body, content_type)):
            try:
                nxt = to_origin_path(url, ref)
            except ValueError as error:
                failures.append(f"{origin_path} -> {error}")
                continue
            if nxt not in seen:
                queue.append(nxt)
    return seen, types, failures


def digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def verify_manifest(manifest: dict, base_url: str, timeout: int) -> int:
    """Fetch every path the manifest recorded and compare it to the recorded hash."""
    base = base_url.rstrip("/")
    files = manifest.get("files") or {}
    if not files:
        print("the manifest records no files — nothing to verify", file=sys.stderr)
        return 2
    print(f"\nverifying {base} against the manifest ({manifest.get('host')})")
    bad = 0
    for origin_path, want in sorted(files.items()):
        status, body, content_type = fetch(base + origin_path, timeout)
        got = digest(body)
        if status != 200 or got != want["sha256"]:
            print(f"  MISMATCH {origin_path}: HTTP {status}, sha256={got[:12]} want {want['sha256'][:12]}")
            bad += 1
        elif len(body) != want.get("bytes", len(body)):
            print(f"  MISMATCH {origin_path}: {len(body)} bytes, want {want['bytes']}")
            bad += 1
    if bad:
        print(f"\n{bad} file(s) differ — the name is not serving what was recovered", file=sys.stderr)
        return 1
    print(f"  all {len(files)} file(s) identical")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", help="published name to mirror, e.g. <slug>.studio.olympus.innotel.us")
    parser.add_argument("--dest", required=True, help="directory to write into")
    parser.add_argument("--scheme", default="https")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--apply", action="store_true", help="write the files (default is a dry run)")
    parser.add_argument(
        "--verify-base",
        default="",
        help="fetch this base URL and compare it against the manifest",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="do not crawl or write; verify --dest's existing manifest against --verify-base",
    )
    args = parser.parse_args()

    import os

    if args.verify_only:
        if not args.verify_base:
            print("--verify-only needs --verify-base", file=sys.stderr)
            return 2
        existing = manifest_path(args.dest)
        if not os.path.exists(existing):
            print(f"no manifest at {existing} — mirror the site first", file=sys.stderr)
            return 2
        with open(existing) as handle:
            return verify_manifest(json.load(handle), args.verify_base, args.timeout)

    if not args.host:
        print("--host is required unless --verify-only is given", file=sys.stderr)
        return 2

    base_url = f"{args.scheme}://{args.host}"
    print(f"source: {base_url}")
    print(f"dest:   {args.dest}")
    print(f"mode:   {'APPLY' if args.apply else 'DRY RUN'}")

    files, types, failures = crawl(base_url, args.timeout)
    if not files:
        print("nothing fetched — is the name still answering?", file=sys.stderr)
        return 1

    total = sum(len(b) for b in files.values())
    print(f"\n{len(files)} file(s), {total} bytes\n")
    for origin_path in sorted(files):
        size = len(files[origin_path])
        print(f"  {origin_path:<44} {types.get(origin_path, '?'):<24} {size:>8}  sha256={digest(files[origin_path])[:12]}")

    if failures:
        # Reported even on a successful run: a file the site links to but does not
        # serve is exactly the gap this tool cannot close by itself.
        print(f"\n{len(failures)} reference(s) could not be fetched:")
        for line in failures:
            print(f"  {line}")

    if not args.apply:
        print("\ndry run — nothing written. Re-run with --apply.")
        return 0

    for origin_path, body in files.items():
        path = dest_path(args.dest, origin_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(body)

    manifest = {
        "host": args.host,
        "files": {
            origin_path: {"sha256": digest(body), "bytes": len(body), "content_type": types.get(origin_path, "")}
            for origin_path, body in sorted(files.items())
        },
        "unfetched": failures,
    }
    os.makedirs(args.dest, exist_ok=True)
    manifest_file = manifest_path(args.dest)
    with open(manifest_file, "w") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(f"\nwrote {len(files)} file(s) to {args.dest}")
    print(f"manifest: {manifest_file}")

    if args.verify_base:
        # Only reachable with --apply, where the crawl above just overwrote the
        # directory from --host — so this compares the name against itself unless
        # --host differs from --verify-base. Prefer --verify-only for the real check.
        return verify_manifest(manifest, args.verify_base, args.timeout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
