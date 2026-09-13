"""Tests for scripts/package-website.py.

The script's job splits cleanly into a deterministic half (scaffolding, layout,
archiving) and a slow, environment-dependent half (npm install, vite build). Only
the first half is asserted here: a test suite that ran `npm install` would be a
test suite nobody runs. The one thing borrowed from the second half is a real npm
run, and that lives in scripts/tests as an opt-in, not as a unit test.

What is pinned, and why each one is worth a test:

  * the scaffold never overwrites `src/App.tsx` or a stylesheet the model wrote —
    a scaffold that ate the site would publish an empty page, quietly;
  * `src/main.tsx` imports `./index.css`, so that file must exist even when the
    model wrote none, or the build fails with an error that names our scaffold;
  * the zip holds `dist/` and `src/` together, because handing over only the built
    output is handing over something nobody can maintain;
  * a slug is validated before it is joined, so `--publish` cannot be aimed outside
    the sites root.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

HERE = Path(__file__).resolve().parent


def load_module():
    spec = importlib.util.spec_from_file_location(
        "package_website", HERE.parent / "package-website.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["package_website"] = module
    spec.loader.exec_module(module)
    return module


pkg = load_module()


class ScaffoldTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.app = Path(self._tmp.name)
        (self.app / "src").mkdir(parents=True)
        (self.app / "src" / "App.tsx").write_text(
            "export default function App() { return null; }\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_writes_the_pinned_project_files(self) -> None:
        written = pkg.write_scaffold(self.app, force=False)

        for relative in ("package.json", "vite.config.ts", "tsconfig.json", "index.html"):
            self.assertTrue((self.app / relative).is_file(), relative)
            self.assertIn(relative, written)

    def test_never_replaces_the_model_s_entry_point(self) -> None:
        original = (self.app / "src" / "App.tsx").read_text(encoding="utf-8")
        pkg.write_scaffold(self.app, force=False)
        self.assertEqual((self.app / "src" / "App.tsx").read_text(encoding="utf-8"), original)

    def test_keeps_a_stylesheet_the_model_wrote(self) -> None:
        (self.app / "src" / "index.css").write_text("body { color: red; }\n", encoding="utf-8")
        pkg.write_scaffold(self.app, force=False)
        self.assertEqual(
            (self.app / "src" / "index.css").read_text(encoding="utf-8"), "body { color: red; }\n"
        )

    def test_supplies_the_stylesheet_main_tsx_imports(self) -> None:
        # src/main.tsx imports ./index.css unconditionally. Without this file the
        # build fails inside our own scaffold, which reads as a broken deployment.
        pkg.write_scaffold(self.app, force=False)
        self.assertTrue((self.app / "src" / "index.css").is_file())
        self.assertIn("./index.css", (self.app / "src" / "main.tsx").read_text(encoding="utf-8"))

    def test_is_idempotent(self) -> None:
        pkg.write_scaffold(self.app, force=False)
        # Second run changes nothing, so a rebuild is not a rewrite.
        self.assertEqual(pkg.write_scaffold(self.app, force=False), [])

    def test_leaves_the_scaffold_out_of_the_model_s_way(self) -> None:
        pkg.write_scaffold(self.app, force=False)
        package = (self.app / "package.json").read_text(encoding="utf-8")
        self.assertIn('"react"', package)
        self.assertIn('"vite"', package)
        # The scaffold is the only place a dependency can be declared, so the model
        # is told there is no package to add one to — and this is that package.
        self.assertNotIn("dependencies\": {}", package)


class SlugTests(unittest.TestCase):
    def test_rejects_a_traversal_slug(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Makefile").write_text("", encoding="utf-8")
            for bad in ("../escape", "a/b", "", "UPPER", "-leading"):
                with self.assertRaises(SystemExit):
                    pkg.build_dir(root, bad)

    def test_requires_the_build_to_exist(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Makefile").write_text("", encoding="utf-8")
            (root / "builds").mkdir()
            with self.assertRaises(SystemExit):
                pkg.build_dir(root, "missing-app")

    def test_requires_a_non_empty_entry_point(self) -> None:
        with TemporaryDirectory() as tmp:
            app = Path(tmp)
            (app / "src").mkdir()
            (app / "src" / "App.tsx").write_text("   \n", encoding="utf-8")
            with self.assertRaises(SystemExit):
                pkg.entry_source(app)


class ArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.app = Path(self._tmp.name)
        (self.app / "src").mkdir(parents=True)
        (self.app / "src" / "App.tsx").write_text("export default () => null;\n", encoding="utf-8")
        (self.app / "dist" / "assets").mkdir(parents=True)
        (self.app / "dist" / "index.html").write_text("<html></html>\n", encoding="utf-8")
        (self.app / "dist" / "assets" / "index-abc.js").write_text("x()\n", encoding="utf-8")
        (self.app / "node_modules").mkdir()
        (self.app / "node_modules" / "junk.js").write_text("junk\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_lists_project_files_first_then_dist_then_src(self) -> None:
        pkg.write_scaffold(self.app, force=False)
        rows = pkg.site_files(self.app)
        paths = [row["path"] for row in rows]

        self.assertIn("dist/index.html", paths)
        self.assertIn("src/App.tsx", paths)
        self.assertTrue(all(not path.startswith("node_modules/") for path in paths))
        self.assertLess(paths.index("package.json"), paths.index("dist/index.html"))
        self.assertLess(paths.index("dist/index.html"), paths.index("src/App.tsx"))

    def test_holds_what_it_takes_to_rebuild(self) -> None:
        # A zip with dist/ and src/ but no package.json cannot be installed, which
        # makes it an artifact nobody can maintain.
        pkg.write_scaffold(self.app, force=False)
        paths = {row["path"] for row in pkg.site_files(self.app)}

        for needed in ("package.json", "vite.config.ts", "tsconfig.json", "index.html"):
            self.assertIn(needed, paths)

    def test_zip_round_trips_both_halves(self) -> None:
        rows = pkg.site_files(self.app)
        target = pkg.write_zip(self.app, rows)

        with zipfile.ZipFile(target) as archive:
            names = set(archive.namelist())
            self.assertIn("dist/index.html", names)
            self.assertIn("src/App.tsx", names)
            self.assertEqual(
                archive.read("src/App.tsx").decode(), "export default () => null;\n"
            )

    def test_manifest_counts_only_what_was_built(self) -> None:
        import json

        pkg.write_scaffold(self.app, force=False)
        rows = pkg.site_files(self.app)
        target = pkg.write_manifest(self.app, "demo", rows, None)
        manifest = json.loads(target.read_text(encoding="utf-8"))

        self.assertEqual(manifest["kind"], "website")
        self.assertEqual(manifest["entry"], "dist/index.html")
        self.assertEqual(manifest["dist_files"], 2)
        # src/ only — the scaffold is counted separately, so "source_files" means
        # what the model wrote rather than "everything that is not dist".
        # src/App.tsx (the model's) plus src/main.tsx and src/index.css (ours).
        self.assertEqual(manifest["source_files"], 3)
        self.assertGreater(manifest["project_files"], 0)
        self.assertEqual(
            manifest["dist_files"] + manifest["source_files"] + manifest["project_files"],
            len(rows),
        )
        self.assertIsNone(manifest["zip"])

    def test_publish_copies_only_dist(self) -> None:
        import os

        with TemporaryDirectory() as root:
            os.environ["OLYMPUS_SITES_ROOT"] = root
            try:
                staged = pkg.publish(self.app, "demo")
            finally:
                del os.environ["OLYMPUS_SITES_ROOT"]

            self.assertEqual(staged, Path(root) / "demo")
            self.assertTrue((staged / "index.html").is_file())
            # The served tree is a delivery surface; the source stays in builds/.
            self.assertFalse((staged / "src").exists())

    def test_publish_replaces_a_previous_stage(self) -> None:
        import os

        with TemporaryDirectory() as root:
            os.environ["OLYMPUS_SITES_ROOT"] = root
            try:
                (Path(root) / "demo").mkdir()
                (Path(root) / "demo" / "stale.html").write_text("old\n", encoding="utf-8")
                pkg.publish(self.app, "demo")
            finally:
                del os.environ["OLYMPUS_SITES_ROOT"]

            self.assertFalse((Path(root) / "demo" / "stale.html").exists())

    def test_publish_refuses_without_a_build(self) -> None:
        import shutil

        shutil.rmtree(self.app / "dist")
        with self.assertRaises(SystemExit):
            pkg.publish(self.app, "demo")


if __name__ == "__main__":
    unittest.main()
