# Build Requests — Local Development

Drop a spec here and Olympus manufactures a standalone app into `../builds/`.

**Quick start (local):**

```bash
# 1. Create a request from the template
make new-request NAME=my-todo
# or: cp factory/APP_SPEC_TEMPLATE.md build-requests/my-todo.md && $EDITOR build-requests/my-todo.md

# 2. Fill in Purpose / Stack / Features / Verification in the md file

# 3. Manufacture locally (mirrors the GitHub workflow)
make app SPEC=build-requests/my-todo.md
# or without SPEC → picks the most recent build-requests/*.md
make app

# 4. Check output (gitignored, like .factory/runs/)
ls -la builds/
```

**On push**, `.github/workflows/olympus-app-builder.yml` does the same in CI
(trigger: `push` with `paths: build-requests/*.md`): monolithic setup
(Node/Bun/Python), OmniRoute gateway, `factory.py run archon-greenfield`.

**From Studio**, a spec can be written here instead of copied by hand: open a
saved app and click **Export to factory** (`web/studio/README.md` documents the
route). The spec is assembled from that build — instruction, file set, sizes —
with no second model call, so the same app always produces the same bytes. It
refuses to overwrite a spec that already exists unless you confirm, which keeps a
hand-edited request from being silently replaced by its own export.

- Edit `factory/APP_SPEC_TEMPLATE.md` for the template shape.
- `builds/` is ignored per `.gitignore:builds/` (factory output); `build-requests/*.md` is tracked.
- `build-requests/.cache/` is ignored for local scratch.
