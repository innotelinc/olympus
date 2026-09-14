"""Tests for scripts/package-app.py.

Same split as `test_package_website.py`: the deterministic half is asserted here
and the slow, environment-dependent half (npm install, vite build, docker build)
is not, because a suite that installed a dependency tree is a suite nobody runs.

What is pinned, and why each one is a test rather than a hope:

  * the generated server is the security boundary of every app this produces, so
    the file the packager writes must not be replaceable by model output;
  * the three files the model owns must survive a scaffold run, or a repackage
    publishes an app with no interface;
  * the API the server implements has to match the API the prompt describes —
    that pairing is the contract between `web/studio/lib/omniroute.ts` and this
    script, and a drift between them is a client calling an endpoint that does
    not exist;
  * the archive has to carry the Dockerfile and the server, because a zip of
    `dist/client` alone cannot be run by anyone.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

HERE = Path(__file__).resolve().parent


def load_module():
    spec = importlib.util.spec_from_file_location("package_app", HERE.parent / "package-app.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["package_app"] = module
    spec.loader.exec_module(module)
    return module


pkg = load_module()

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS entries (\n"
    "  id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
    "  label TEXT NOT NULL,\n"
    "  weight_kg REAL NOT NULL\n"
    ");\n"
)
APP_TSX = "export default function App() { return null; }\n"


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.app = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write_app(self, *, schema: str | None = SCHEMA, app: str | None = APP_TSX) -> None:
        if schema is not None:
            (self.app / "server").mkdir(parents=True, exist_ok=True)
            (self.app / "server" / "schema.sql").write_text(schema, encoding="utf-8")
        if app is not None:
            (self.app / "src").mkdir(parents=True, exist_ok=True)
            (self.app / "src" / "App.tsx").write_text(app, encoding="utf-8")


class ScaffoldTests(Fixture):
    def test_writes_the_project_the_model_is_not_allowed_to_write(self) -> None:
        self.write_app()
        written = pkg.write_scaffold(self.app, force=False)

        for relative in (
            "package.json",
            "vite.config.ts",
            "tsconfig.json",
            "index.html",
            "src/main.tsx",
            "server/main.ts",
            "Dockerfile",
            ".dockerignore",
        ):
            self.assertTrue((self.app / relative).is_file(), relative)
            self.assertIn(relative, written)

    def test_keeps_the_files_the_model_owns(self) -> None:
        self.write_app()
        pkg.write_scaffold(self.app, force=False)

        self.assertEqual((self.app / "src" / "App.tsx").read_text(encoding="utf-8"), APP_TSX)
        self.assertEqual((self.app / "server" / "schema.sql").read_text(encoding="utf-8"), SCHEMA)

    def test_replaces_a_server_or_dockerfile_the_model_emitted(self) -> None:
        # Not a nicety: `server/main.ts` is the request path, and a model-written
        # one is exactly what generating it exists to prevent.
        self.write_app()
        (self.app / "server" / "main.ts").write_text("// mine\n", encoding="utf-8")
        (self.app / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")

        pkg.write_scaffold(self.app, force=False)

        self.assertNotIn("// mine", (self.app / "server" / "main.ts").read_text(encoding="utf-8"))
        self.assertIn("FROM node:", (self.app / "Dockerfile").read_text(encoding="utf-8"))

    def test_is_idempotent(self) -> None:
        self.write_app()
        pkg.write_scaffold(self.app, force=False)
        second = pkg.write_scaffold(self.app, force=False)
        # The second run writes nothing: same bytes, so the write is skipped and
        # the lockfile keeps meaning something.
        self.assertEqual(second, [])

    def test_index_css_is_written_only_when_the_model_wrote_none(self) -> None:
        self.write_app()
        (self.app / "src" / "index.css").write_text("body { color: red; }\n", encoding="utf-8")

        pkg.write_scaffold(self.app, force=False)

        self.assertEqual(
            (self.app / "src" / "index.css").read_text(encoding="utf-8"),
            "body { color: red; }\n",
        )


class ServerContractTests(Fixture):
    """The generated server, checked where it can be checked without running it."""

    def test_the_runtime_image_carries_no_dependencies(self) -> None:
        server = pkg.SERVER_MAIN
        # Only built-ins: an app whose runtime stage has no node_modules has
        # nothing to install at boot and no third-party code behind a public route.
        for imported in server.split("from \"")[1:]:
            module = imported.split("\"")[0]
            self.assertTrue(module.startswith("node:"), module)

    def test_the_client_build_lands_where_the_dockerfile_copies_from(self) -> None:
        self.assertEqual(pkg.ENTRY_BUILT, "dist/client/index.html")
        self.assertIn("dist/client", pkg.VITE_CONFIG)
        self.assertIn("COPY --from=build /app/dist/client ./public", pkg.DOCKERFILE)

    def test_it_serves_the_client_and_the_api_from_one_origin(self) -> None:
        # The prompt tells the model to fetch relative `/api/...` paths, so the
        # server has to answer both on the same port.
        self.assertIn('url.pathname === "/api/health"', pkg.SERVER_MAIN)
        self.assertIn("serveStatic", pkg.SERVER_MAIN)

    def test_it_implements_every_verb_the_generated_api_promises(self) -> None:
        # The prose contract this used to check against lived in Studio's prompt for
        # the fixed stack, and that prompt is gone: generation is planned now, and a
        # planned project writes its own server. This packager still *builds* the
        # projects that were generated against it — the apps saved before the planner
        # — so what is asserted is the API it generates, not a prompt that described it.
        for verb in ("GET", "POST", "PATCH", "PUT", "DELETE"):
            self.assertIn(f'request.method === "{verb}"', pkg.SERVER_MAIN)

    def test_an_unknown_identifier_is_refused_rather_than_queried(self) -> None:
        # Quoting alone would stop injection but not a request for a table that is
        # not there, which should be a 404 rather than an empty list.
        self.assertIn("TABLES.includes(table)", pkg.SERVER_MAIN)
        self.assertIn("unknown field(s)", pkg.SERVER_MAIN)

    def test_static_paths_cannot_escape_the_public_directory(self) -> None:
        self.assertIn("PUBLIC_DIR + sep", pkg.SERVER_MAIN)

    def test_the_database_lives_outside_the_image(self) -> None:
        # The data has to survive a rebuild: it is on a volume the runtime mount
        # points at, not in the container's writable layer.
        self.assertIn('DATA_DIR = process.env.DATA_DIR ?? "/data"', pkg.SERVER_MAIN)
        self.assertIn('VOLUME ["/data"]', pkg.DOCKERFILE)


class ArchiveTests(Fixture):
    def setUp(self) -> None:
        super().setUp()
        self.write_app()
        pkg.write_scaffold(self.app, force=False)
        (self.app / "dist" / "client" / "assets").mkdir(parents=True)
        (self.app / "dist" / "client" / "index.html").write_text("<html></html>\n", encoding="utf-8")
        (self.app / "dist" / "client" / "assets" / "index.js").write_text("x\n", encoding="utf-8")
        (self.app / "package-lock.json").write_text("{}\n", encoding="utf-8")

    def test_the_archive_can_be_run_and_rebuilt(self) -> None:
        rows = pkg.app_files(self.app)
        paths = {row["path"] for row in rows}

        # Each of these is load-bearing for whoever receives the zip: the
        # Dockerfile and the server are how it runs, the lockfile and configs are
        # how it rebuilds, and dist/ is what runs now.
        for expected in (
            "Dockerfile",
            "package.json",
            "package-lock.json",
            "vite.config.ts",
            "tsconfig.json",
            "index.html",
            "server/main.ts",
            "server/schema.sql",
            "src/App.tsx",
            "dist/client/index.html",
        ):
            self.assertIn(expected, paths)

    def test_the_zip_really_contains_them(self) -> None:
        rows = pkg.app_files(self.app)
        archive = pkg.write_zip(self.app, rows)

        with zipfile.ZipFile(archive) as opened:
            names = set(opened.namelist())

        self.assertIn("Dockerfile", names)
        self.assertIn("server/main.ts", names)
        self.assertIn("dist/client/index.html", names)

    def test_the_manifest_says_it_is_an_app(self) -> None:
        rows = pkg.app_files(self.app)
        manifest = json.loads(pkg.write_manifest(self.app, "probe-app", rows, None).read_text())

        self.assertEqual(manifest["kind"], "app")
        self.assertEqual(manifest["slug"], "probe-app")
        self.assertEqual(manifest["entry"], "dist/client/index.html")
        self.assertEqual(manifest["image"], pkg.image_tag("probe-app"))
        self.assertGreaterEqual(manifest["dist_files"], 2)


class EntryTests(Fixture):
    def test_a_missing_schema_is_refused_with_the_reason(self) -> None:
        self.write_app(schema=None)
        with self.assertRaises(SystemExit) as raised:
            pkg.require_entry(self.app, pkg.ENTRY_SCHEMA, "that is the app's data model")
        self.assertEqual(raised.exception.code, 2)

    def test_an_empty_client_is_refused(self) -> None:
        self.write_app(app="   \n")
        with self.assertRaises(SystemExit):
            pkg.require_entry(self.app, pkg.ENTRY_CLIENT, "that is the app's interface")


class SlugTests(Fixture):
    def test_unsafe_slugs_are_refused(self) -> None:
        for slug in ("../escape", "a/b", "UPPER", "-leading", "", "x" * 61):
            with self.assertRaises(SystemExit, msg=slug):
                pkg.build_dir(Path("/tmp"), slug)

    def test_a_missing_build_names_the_command_that_makes_one(self) -> None:
        with TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit) as raised:
                pkg.build_dir(Path(tmp) / "repo", "nothing-here")
            self.assertEqual(raised.exception.code, 2)


class ImageTaggingTests(unittest.TestCase):
    def test_the_tag_is_derived_from_the_slug_and_nothing_else(self) -> None:
        # `app-runtime.py` runs the container by this name and `build-runner.py`
        # reports it. One derivation, so they cannot disagree about what published.
        self.assertEqual(pkg.image_tag("weight-tracker"), "olympus-app-weight-tracker:latest")


class LockfileTests(Fixture):
    """`npm ci` only runs against a lockfile that belongs to this project.

    npm trusts a lockfile completely: it refuses when the lock and the manifest
    disagree, and when they agree it installs the locked tree whether or not that tree
    is complete. So a lockfile the scaffold did not write is either a stop or a quiet
    partial install in the image, and the packager has to be able to tell.
    """

    def write_lock(self, root: dict) -> None:
        (self.app / "package-lock.json").write_text(
            json.dumps({"lockfileVersion": 3, "packages": {"": root}}), encoding="utf-8"
        )

    def test_a_lockfile_npm_built_from_this_manifest_is_ours(self) -> None:
        ours = json.loads(pkg.PACKAGE_JSON)
        self.write_lock({field: ours.get(field) for field in pkg.ROOT_PACKAGE_FIELDS})
        self.assertTrue(pkg.lockfile_is_ours(self.app))

    def test_a_lockfile_for_a_different_dependency_set_is_not_ours(self) -> None:
        ours = json.loads(pkg.PACKAGE_JSON)
        root = {field: ours.get(field) for field in pkg.ROOT_PACKAGE_FIELDS}
        root["dependencies"] = {"better-sqlite3": "13.0.3"}
        self.write_lock(root)
        self.assertFalse(pkg.lockfile_is_ours(self.app))

    def test_a_missing_or_unreadable_lockfile_is_not_ours(self) -> None:
        self.assertFalse(pkg.lockfile_is_ours(self.app))

        for contents in ("{not json", "[]", '{"packages": {}}'):
            with self.subTest(contents=contents):
                (self.app / "package-lock.json").write_text(contents, encoding="utf-8")
                self.assertFalse(pkg.lockfile_is_ours(self.app))


class OutputTests(unittest.TestCase):
    """The scaffold has to be valid text for the tools that read it."""

    def test_the_package_json_parses_and_pins_the_client_toolchain(self) -> None:
        payload = json.loads(pkg.PACKAGE_JSON)
        self.assertEqual(payload["scripts"]["build"], "tsc -b && vite build")
        self.assertIn("vite", payload["devDependencies"])
        self.assertIn("react", payload["dependencies"])

    def test_the_dockerfile_is_two_stages_so_the_runtime_has_no_toolchain(self) -> None:
        self.assertEqual(pkg.DOCKERFILE.count("FROM "), 2)
        self.assertIn("RUN npm ci", pkg.DOCKERFILE)
        self.assertIn("HEALTHCHECK", pkg.DOCKERFILE)

    def test_only_the_build_stage_carries_a_toolchain(self) -> None:
        # `npm ci` installs the locked tree whether or not it is complete, so a
        # dependency with no prebuilt binary for musl has to compile — and the base
        # image has no compiler. The runtime stage is a fresh image, so the published
        # app keeps none of it; that is the whole reason the build is two stages.
        _, _, after_header = pkg.DOCKERFILE.partition("\nFROM ")
        build_stage, _, runtime_stage = after_header.partition("\nFROM ")
        self.assertIn("apk add", build_stage)
        self.assertNotIn("apk add", runtime_stage)


if __name__ == "__main__":
    unittest.main()
