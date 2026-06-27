# Git Workflow

## Branches

Use `main` as the stable branch. Code on `main` should be safe to pull onto the
Raspberry Pi.

Use short-lived topic branches for work:

```text
feature/<short-description>
fix/<short-description>
docs/<short-description>
chore/<short-description>
```

Examples:

```text
feature/sen66-gateway-decode
fix/mqtt-reconnect
docs/pi-clean-install
chore/root-gitignore
```

## Commits

Use concise imperative commit messages:

```text
Add root README
Ignore ESP-IDF build artifacts
Document Raspberry Pi deployment
```

Keep commits focused. A firmware behavior change, backend behavior change, and
documentation cleanup should usually be separate commits.

## Pull Requests

Use pull requests for merges into `main`, even for solo development. A pull
request gives you a review point for:

- What changed
- Which hardware or backend checks were run
- Whether deployment instructions changed
- Whether secrets or generated files accidentally entered the diff

Before merging, check:

```sh
git status --short
git diff --stat main
```

Run the relevant validation:

- ESP-IDF build for changed firmware projects
- Backend tests for changed Python code
- Raspberry Pi install or service verification for deployment changes

## Releases

Tag known-good versions after validation.

For date-based home deployments:

```sh
git tag -a v2026.06.27 -m "Raspberry Pi deployment baseline"
git push origin v2026.06.27
```

If the project later needs formal compatibility tracking across firmware,
backend, and hardware revisions, switch to semantic versioning:

```text
v1.0.0
v1.1.0
v1.1.1
```

## GitHub Actions

Do not add GitHub Actions until there is a clear validation target. Embedded
firmware and Raspberry Pi services often need hardware-aware checks, so local
build and deployment verification should come first.
