"""Tests for scripts/package-project.py.

The docker build itself is exercised end to end against real images rather than
here — what is asserted here is everything that decides what that build is:
which base image, which commands, which port, and what PID 1 ends up being.

Each of those failures is quiet in a different way:

  * the wrong base image is a build error about a binary nobody asked for;
  * a missing or reordered command is an image that builds and does not run;
  * an unknown language written straight into `FROM` is a docker error that names
    a repository, not the plan;
  * a start command in the wrong `CMD` form runs fine and then loses its database
    to `docker stop`, which is the one nobody finds by testing.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

HERE = Path(__file__).resolve().parent


def load_module():
    spec = importlib.util.spec_from_file_location(
        "package_project", HERE.parent / "package-project.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["package_project"] = module
    spec.loader.exec_module(module)
    return module


packager = load_module()


def write_plan(directory: Path, **overrides) -> dict:
    plan = {
        "name": "Weight Tracker",
        "kind": "app",
        "summary": "Daily weigh-ins.",
        "runtime": {"language": "python", "frameworks": ["flask"], "database": "sqlite"},
        "run": {
            "install": "pip install -r requirements.txt",
            "build": "",
            "start": "python app.py",
            "port": 8000,
            "healthcheck": "/healthz",
        },
        "notes": None,
    }
    for key, value in overrides.items():
        if key in ("runtime", "run") and isinstance(value, dict):
            plan[key] = {**plan[key], **value}
        else:
            plan[key] = value

    (directory / packager.PLAN_NAME).write_text(json.dumps(plan), encoding="utf-8")
    return plan


class Language(unittest.TestCase):
    def test_accepts_the_supported_languages(self):
        for language in packager.BASE_IMAGES:
            self.assertEqual(packager.normalise_language(language), language)

    def test_accepts_a_planner_writing_the_framework_instead_of_the_language(self):
        # "Flask" is not a language, it is the thing the language has. Refusing it
        # would be pedantry with a failed build attached.
        self.assertEqual(packager.normalise_language("Flask"), "python")
        self.assertEqual(packager.normalise_language("TypeScript"), "node")
        self.assertEqual(packager.normalise_language("Golang"), "go")

    def test_is_case_and_space_insensitive(self):
        self.assertEqual(packager.normalise_language("  Node  "), "node")

    def test_returns_none_for_something_it_cannot_build(self):
        for value in ("rust", "java", "", None, 42, "cobol"):
            self.assertIsNone(packager.normalise_language(value))

    def test_every_base_image_is_pinned_and_alpine(self):
        # A floating tag turns "this project builds" into a coin flip, and a
        # non-alpine base has no busybox `wget` for the healthcheck to use.
        for image in packager.BASE_IMAGES.values():
            self.assertIn("alpine", image)
            self.assertRegex(image, r":\d")


class PlanValidation(unittest.TestCase):
    def plan_in(self, directory: Path, **overrides) -> dict:
        write_plan(directory, **overrides)
        return packager.plan_for_build(directory)

    def test_reads_the_run_commands_and_port(self):
        with TemporaryDirectory() as raw:
            plan = self.plan_in(Path(raw))
            self.assertEqual(plan["language"], "python")
            self.assertEqual(plan["install"], "pip install -r requirements.txt")
            self.assertEqual(plan["start"], "python app.py")
            self.assertEqual(plan["port"], 8000)
            self.assertEqual(plan["healthcheck"], "/healthz")

    def test_refuses_a_language_with_no_image(self):
        with TemporaryDirectory() as raw:
            directory = Path(raw)
            write_plan(directory)
            payload = json.loads((directory / packager.PLAN_NAME).read_text())
            payload["runtime"]["language"] = "rust"
            (directory / packager.PLAN_NAME).write_text(json.dumps(payload))
            with self.assertRaises(SystemExit) as caught:
                packager.plan_for_build(directory)
            self.assertEqual(caught.exception.code, 2)

    def test_refuses_a_plan_with_no_start_command(self):
        with TemporaryDirectory() as raw:
            with self.assertRaises(SystemExit) as caught:
                self.plan_in(Path(raw), run={"start": "   "})
            self.assertEqual(caught.exception.code, 2)

    def test_refuses_static_with_something_to_install(self):
        # nginx is not a toolchain, and `npm: not found` is not a message about the
        # plan that asked for it.
        with TemporaryDirectory() as raw:
            with self.assertRaises(SystemExit):
                self.plan_in(Path(raw), runtime={"language": "static"}, run={"install": "npm ci"})

    def test_refuses_a_port_outside_the_usable_range(self):
        for port in (80, 0, -1, 70000):
            with TemporaryDirectory() as raw:
                with self.assertRaises(SystemExit):
                    self.plan_in(Path(raw), run={"port": port})

    def test_collapses_a_command_onto_one_line(self):
        # A newline inside `RUN` is a Dockerfile continuation and would join the next
        # instruction onto this command.
        with TemporaryDirectory() as raw:
            plan = self.plan_in(Path(raw), run={"install": "pip install  \n  -r  req.txt"})
            self.assertEqual(plan["install"], "pip install -r req.txt")

    def test_defaults_a_healthcheck_that_is_not_a_path(self):
        with TemporaryDirectory() as raw:
            for value in ("http://example.com", "", None, "health"):
                plan = self.plan_in(Path(raw), run={"healthcheck": value})
                self.assertEqual(plan["healthcheck"], "/")

    def test_strips_a_query_string_from_the_healthcheck(self):
        with TemporaryDirectory() as raw:
            plan = self.plan_in(Path(raw), run={"healthcheck": "/health?deep=1"})
            self.assertEqual(plan["healthcheck"], "/health")

    def test_refuses_a_missing_plan(self):
        with TemporaryDirectory() as raw:
            with self.assertRaises(SystemExit):
                packager.plan_for_build(Path(raw))


class StartCommand(unittest.TestCase):
    def test_a_plain_command_uses_the_exec_form(self):
        # No shell at all, so the application is PID 1 and `docker stop` reaches it.
        self.assertEqual(packager.start_instruction("python app.py"), 'CMD ["python", "app.py"]')
        self.assertEqual(packager.start_instruction("npm start"), 'CMD ["npm", "start"]')

    def test_quoted_arguments_survive_the_exec_form(self):
        self.assertEqual(
            packager.start_instruction('node -e "console.log(1)"'),
            'CMD ["node", "-e", "console.log(1)"]',
        )

    def test_a_compound_command_keeps_the_shell(self):
        # `exec` here would replace the shell with `npm run migrate` and the server
        # would never start.
        text = packager.start_instruction("npm run migrate && npm start")
        self.assertEqual(text, "CMD npm run migrate && npm start")
        self.assertNotIn("exec", text)

    def test_a_variable_prefix_keeps_the_shell_and_execs(self):
        # The JSON form would try to execute a binary called `PORT=3000`.
        self.assertEqual(
            packager.start_instruction("PORT=3000 node app.js"), "CMD exec PORT=3000 node app.js"
        )

    def test_a_pipe_keeps_the_shell(self):
        self.assertIn("|", packager.start_instruction("node app.js | tee log"))


class Dockerfile(unittest.TestCase):
    def dockerfile_for(self, **overrides) -> str:
        with TemporaryDirectory() as raw:
            directory = Path(raw)
            write_plan(directory, **overrides)
            return packager.dockerfile_for(packager.plan_for_build(directory))

    def test_uses_the_base_image_for_the_language(self):
        self.assertIn("FROM python:3.13-alpine", self.dockerfile_for())
        self.assertIn(
            "FROM golang:1.24-alpine",
            self.dockerfile_for(runtime={"language": "go"}),
        )

    def test_runs_install_then_build_then_start_in_order(self):
        text = self.dockerfile_for(run={"install": "npm ci", "build": "npm run build"})
        self.assertLess(text.index("RUN npm ci"), text.index("RUN npm run build"))
        self.assertLess(text.index("RUN npm run build"), text.index('CMD ["python", "app.py"]'))

    def test_omits_an_empty_install_or_build(self):
        text = self.dockerfile_for(run={"install": "", "build": ""})
        self.assertNotIn("RUN ", text)

    def test_every_language_that_installs_something_can_compile_it(self):
        # Lockfiles are ignored, so each tree comes from the manifest the model wrote,
        # and a manifest may name something with no prebuilt binary for musl: a node
        # module, a C extension, a cgo import, a native gem. The base images are the
        # official ones — they carry their interpreter's own headers and no compiler,
        # which is the half these add. Verified against each image.
        for language, packages in (
            ("node", "python3 make g++"),
            ("python", "build-base"),
            ("go", "build-base"),
            ("php", "$PHPIZE_DEPS"),
            ("ruby", "build-base"),
        ):
            with self.subTest(language=language):
                text = self.dockerfile_for(runtime={"language": language})
                self.assertIn(f"RUN apk add --no-cache {packages}", text)

    def test_the_toolchain_lands_before_the_source_so_it_stays_cached(self):
        # After `COPY . .` every source change would invalidate it, and each build
        # would pay for the toolchain again.
        text = self.dockerfile_for(
            runtime={"language": "node"},
            run={"install": "npm install", "start": "node server/index.js"},
        )
        self.assertLess(text.index("apk add"), text.index("COPY . ."))
        self.assertLess(text.index("apk add"), text.index("RUN npm install"))

    def test_a_static_site_has_nothing_to_compile_so_gets_no_toolchain(self):
        text = self.dockerfile_for(
            runtime={"language": "static"},
            run={"install": "", "build": "", "start": "nginx -g 'daemon off;'"},
        )
        self.assertNotIn("apk add", text)

    def split(self, text):
        """The text before the runtime stage, and the runtime stage itself."""
        build_stage, _, runtime_stage = text.partition("\nFROM node:24-alpine\n")
        self.assertNotEqual(runtime_stage, "", "this build was not split into two stages")
        return build_stage, runtime_stage

    def test_a_node_build_splits_so_the_toolchain_never_reaches_the_runtime(self):
        # `npm install` writes `node_modules` into the workdir, so the workdir is the
        # result and the runtime stage can be a fresh base image plus a copy of it.
        text = self.dockerfile_for(
            runtime={"language": "node"},
            run={"install": "npm install", "start": "node server/index.js"},
        )
        self.assertEqual(text.count("FROM "), 2)

        build_stage, runtime_stage = self.split(text)
        self.assertIn("apk add", build_stage)
        self.assertNotIn("apk add", runtime_stage)
        self.assertIn("COPY --from=build /app /app", runtime_stage)

    def test_a_language_whose_install_lands_outside_the_project_is_not_split(self):
        # `pip install` writes to site-packages, not into the workdir, so a runtime
        # stage copied from the build stage would be missing everything the install
        # produced — the app would start and fail on its first route. One stage, and
        # the toolchain is the price of that.
        text = self.dockerfile_for()
        self.assertEqual(text.count("FROM "), 1)
        self.assertIn("apk add", text)

    def test_the_runtime_stage_names_the_port_and_the_start_command_again(self):
        # `ENV` does not carry across a `FROM`. Losing it would run the app on the
        # port the image defaults to, which nothing publishes.
        text = self.dockerfile_for(
            runtime={"language": "node"},
            run={"install": "npm install", "start": "node server/index.js", "port": 3200},
        )
        _, runtime_stage = self.split(text)
        self.assertIn("ENV PORT=3200", runtime_stage)
        self.assertIn("EXPOSE 3200", runtime_stage)
        self.assertIn('CMD ["node", "server/index.js"]', runtime_stage)
        self.assertIn("HEALTHCHECK", runtime_stage)

    def test_gives_the_project_port_host_and_data_dir(self):
        text = self.dockerfile_for(run={"port": 8100})
        self.assertIn("ENV PORT=8100", text)
        self.assertIn("HOST=0.0.0.0", text)
        self.assertIn("DATA_DIR=/data", text)
        self.assertIn("EXPOSE 8100", text)
        self.assertIn('VOLUME ["/data"]', text)

    def test_healthcheck_asks_the_plans_own_path(self):
        text = self.dockerfile_for(run={"healthcheck": "/api/up", "port": 8100})
        self.assertIn('http://127.0.0.1:8100/api/up', text)

    def test_a_static_site_is_served_by_nginx_on_the_plans_port(self):
        text = self.dockerfile_for(
            runtime={"language": "static"},
            run={"install": "", "build": "", "start": "nginx -g 'daemon off;'"},
        )
        self.assertIn("FROM nginx:1.29-alpine", text)
        self.assertIn("listen 8000", text)
        self.assertIn("root /usr/share/nginx/html", text)
        self.assertIn('CMD ["nginx", "-g", "daemon off;"]', text)

    def test_static_does_not_start_the_projects_own_command(self):
        # There is no process to start; nginx is the server. Running the plan's
        # `start` would be a second, contradictory server on the same port.
        text = self.dockerfile_for(
            runtime={"language": "static"},
            run={"install": "", "build": "", "start": "python app.py"},
        )
        # Named in the header comment as what the plan said, never executed.
        self.assertNotIn("CMD python app.py", text)
        self.assertIn('CMD ["nginx", "-g", "daemon off;"]', text)


class Dockerignore(unittest.TestCase):
    def test_keeps_the_hosts_installs_out_of_the_image(self):
        # A copied-in node_modules would shadow what `install` resolves, and the
        # image would carry whatever the last host install happened to leave.
        for entry in ("node_modules", ".venv", "__pycache__", "vendor"):
            self.assertIn(entry, packager.DOCKERIGNORE.splitlines())

    def test_keeps_its_own_output_out_of_the_context(self):
        for entry in ("Dockerfile", "plan.json", packager.MANIFEST_NAME):
            self.assertIn(entry, packager.DOCKERIGNORE.splitlines())

    def test_keeps_a_lockfile_it_did_not_write_out_of_the_image(self):
        # This script reads the plan and never writes a lockfile, so one in the
        # directory is unvouched-for input — and npm trusts it completely. A tree
        # that does not match the manifest makes the install compile from source
        # instead of taking the prebuilt binary, which no base image here can do.
        self.assertIn("package-lock.json", packager.DOCKERIGNORE.splitlines())


class DockerfileIsWritten(unittest.TestCase):
    def test_a_model_written_dockerfile_is_replaced(self):
        # The Dockerfile is this script's, unconditionally: a model that emitted one
        # would otherwise be built, and the runtime the plan exists to pin would be
        # whatever it wrote.
        with TemporaryDirectory() as raw:
            directory = Path(raw)
            write_plan(directory)
            (directory / "Dockerfile").write_text("FROM scratch\n")
            packager.write_dockerfile(directory, packager.plan_for_build(directory))
            self.assertIn("FROM python:3.13-alpine", (directory / "Dockerfile").read_text())


class Archive(unittest.TestCase):
    def test_skips_its_own_output_and_keeps_the_project(self):
        with TemporaryDirectory() as raw:
            directory = Path(raw)
            write_plan(directory)
            (directory / "app.py").write_text("print('hi')\n")
            (directory / "Dockerfile").write_text("FROM x\n")
            packager.write_manifest(
                directory, "x", packager.plan_for_build(directory), [], None
            )

            rows = packager.project_files(directory)
            paths = {row["path"] for row in rows}

            self.assertIn("app.py", paths)
            # The Dockerfile and the plan go with the project: they are what makes the
            # download runnable by somebody who was not watching it be built.
            self.assertIn("Dockerfile", paths)
            self.assertIn(packager.PLAN_NAME, paths)
            self.assertNotIn(packager.MANIFEST_NAME, paths)

    def test_zips_the_project(self):
        with TemporaryDirectory() as raw:
            directory = Path(raw)
            write_plan(directory)
            (directory / "app.py").write_text("print('hi')\n")

            rows = packager.project_files(directory)
            target = packager.write_zip(directory, rows)

            self.assertIsNotNone(target)
            assert target is not None
            import zipfile

            with zipfile.ZipFile(target) as archive:
                self.assertIn("app.py", archive.namelist())


class ConvexTarget(unittest.TestCase):
    """A plan whose state lives on Convex rather than inside its own container.

    Two things make the target real instead of a word in a plan: the client is
    pointed at the deployment under every name a framework reads, and the one
    credential a build may use arrives as a build argument rather than an `ENV`.
    """

    def plan_for(
        self, directory: Path, env: dict[str, str] | None = None, **overrides
    ) -> dict:
        write_plan(directory, target="convex", **overrides)
        with patch.dict(
            os.environ,
            env if env is not None else {"CONVEX_URL": "https://convex.example"},
            clear=False,
        ):
            return packager.plan_for_build(directory)

    def test_defaults_to_the_container_every_project_was_before(self):
        with TemporaryDirectory() as raw:
            directory = Path(raw)
            write_plan(directory)
            plan = packager.plan_for_build(directory)
            self.assertEqual(plan["target"], "container")
            self.assertEqual(plan["convex_url"], "")

    def test_reads_the_target_and_the_deployment_it_points_at(self):
        with TemporaryDirectory() as raw:
            plan = self.plan_for(Path(raw))
            self.assertEqual(plan["target"], "convex")
            self.assertEqual(plan["convex_url"], "https://convex.example")

    def test_refuses_a_convex_plan_with_no_deployment_to_point_at(self):
        # The failure this prevents is quiet: a container that serves a client whose
        # deployment URL is empty, so every read comes back blank.
        with TemporaryDirectory() as raw:
            with self.assertRaises(SystemExit) as caught:
                self.plan_for(Path(raw), env={"CONVEX_URL": "   "})
            self.assertEqual(caught.exception.code, 2)

    def test_refuses_a_static_site_targeting_convex(self):
        # nginx serving files has nothing that can deploy a function, and a site whose
        # schema never landed is worse than a refusal that says so.
        with TemporaryDirectory() as raw:
            with self.assertRaises(SystemExit) as caught:
                self.plan_for(
                    Path(raw),
                    runtime={"language": "static"},
                    run={"install": "", "build": ""},
                )
            self.assertEqual(caught.exception.code, 2)

    def test_points_the_client_at_the_deployment_under_every_name(self):
        # A Vite client only sees `VITE_`, a Next client only sees `NEXT_PUBLIC_`, and
        # Convex's own tooling sees neither — so all three are written, and the value
        # is the operator's, never a hardcoded one.
        with TemporaryDirectory() as raw:
            text = packager.dockerfile_for(self.plan_for(Path(raw)))
            for key in packager.CONVEX_URL_KEYS:
                self.assertIn(f"{key}=https://convex.example", text)
            self.assertEqual(text.count("NEXT_PUBLIC_CONVEX_URL=https://convex.example"), 1)

    def test_the_deployment_is_written_into_both_stages_when_the_build_is_split(self):
        with TemporaryDirectory() as raw:
            plan = self.plan_for(
                Path(raw),
                runtime={"language": "node", "database": "convex"},
                run={"install": "npm ci", "build": "npx convex deploy && npm run build", "start": "node server.js"},
            )
            text = packager.dockerfile_for(plan)

            self.assertEqual(text.count("FROM node:24-alpine"), 2)
            # `ENV` does not carry across a `FROM`, and the runtime stage is the one
            # that serves the client.
            self.assertEqual(text.count("NEXT_PUBLIC_CONVEX_URL=https://convex.example"), 2)

    def test_the_deploy_key_is_a_build_argument_and_never_an_env(self):
        with TemporaryDirectory() as raw:
            text = packager.dockerfile_for(self.plan_for(Path(raw)))
            self.assertIn(f"ARG {packager.CONVEX_DEPLOY_KEY}", text)
            self.assertNotIn(f"ENV {packager.CONVEX_DEPLOY_KEY}", text)

    def test_a_container_plan_gets_none_of_it(self):
        with TemporaryDirectory() as raw:
            directory = Path(raw)
            write_plan(directory)
            text = packager.dockerfile_for(packager.plan_for_build(directory))
            self.assertNotIn("CONVEX", text)

    def test_the_deploy_key_reaches_docker_only_when_there_is_one(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(packager.convex_build_args({"target": "convex"}), [])
            self.assertEqual(packager.convex_build_args({"target": "container"}), [])

        with patch.dict(os.environ, {packager.CONVEX_DEPLOY_KEY: "prod:scoped"}, clear=True):
            self.assertEqual(
                packager.convex_build_args({"target": "convex"}),
                ["--build-arg", f"{packager.CONVEX_DEPLOY_KEY}=prod:scoped"],
            )
            # A container plan never gets the key, however loudly it is exported.
            self.assertEqual(packager.convex_build_args({"target": "container"}), [])

    def test_the_manifest_names_the_target(self):
        with TemporaryDirectory() as raw:
            directory = Path(raw)
            plan = self.plan_for(directory)
            packager.write_manifest(directory, "weight-tracker", plan, [], None)
            manifest = json.loads((directory / packager.MANIFEST_NAME).read_text())
            self.assertEqual(manifest["target"], "convex")


class Manifest(unittest.TestCase):
    def test_reports_what_the_runtime_needs(self):
        with TemporaryDirectory() as raw:
            directory = Path(raw)
            write_plan(directory)
            plan = packager.plan_for_build(directory)
            (directory / "app.py").write_text("x")

            rows = packager.project_files(directory)
            packager.write_manifest(directory, "weight-tracker", plan, rows, None)

            manifest = json.loads((directory / packager.MANIFEST_NAME).read_text())
            self.assertEqual(manifest["language"], "python")
            self.assertEqual(manifest["port"], 8000)
            self.assertEqual(manifest["healthcheck"], "/healthz")
            self.assertEqual(manifest["image"], "olympus-app-weight-tracker:latest")
            self.assertEqual(manifest["kind"], "app")
            self.assertEqual(manifest["target"], "container")


class Cli(unittest.TestCase):
    def test_refuses_a_slug_it_would_not_publish(self):
        # These are refused before anything is read, which is the point: a slug
        # becomes a hostname, and `..` in one is how a build writes outside builds/.
        for slug in ("../etc", "Upper", "-leading", "with space", ""):
            with self.assertRaises(SystemExit) as caught:
                packager.main([slug, "--no-build"])
            self.assertEqual(caught.exception.code, 2)

    def test_refuses_a_slug_that_escapes_the_builds_directory(self):
        with TemporaryDirectory() as raw:
            root = Path(raw)
            (root / packager.BUILDS_DIR).mkdir()
            (root / "outside").mkdir()
            with self.assertRaises(SystemExit) as caught:
                packager.build_dir(root, "..")
            self.assertEqual(caught.exception.code, 2)

    def test_refuses_a_build_that_is_not_there(self):
        with TemporaryDirectory() as raw:
            root = Path(raw)
            (root / packager.BUILDS_DIR).mkdir()
            with self.assertRaises(SystemExit) as caught:
                packager.build_dir(root, "missing")
            self.assertEqual(caught.exception.code, 2)

    def test_accepts_a_build_inside_the_builds_directory(self):
        with TemporaryDirectory() as raw:
            root = Path(raw)
            (root / packager.BUILDS_DIR / "weight-tracker").mkdir(parents=True)
            found = packager.build_dir(root, "weight-tracker")
            self.assertEqual(found.name, "weight-tracker")


if __name__ == "__main__":
    unittest.main()
