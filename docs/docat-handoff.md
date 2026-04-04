# Docat Handoff

This project publishes documentation to self-hosted Docat as a single zip artifact.

## Deliverable

- Artifact path: `docs-dist/ai-vfs-docs.zip`
- Source: generated from MkDocs build output in `site/`
- Constraint: zip root MUST contain `index.html` (not a top-level `site/` folder)

## Build and package workflow

```bash
uv run --group docs mkdocs build
mkdir -p docs-dist
rm -f docs-dist/ai-vfs-docs.zip
(cd site && zip -qr ../docs-dist/ai-vfs-docs.zip .)
```

## Upload handoff to Docat

Set deployment variables in `.env`:

```bash
DOCAT_HOST="https://docat.example.com"
DOCAT_PROJECT="ai-vfs"
DOCAT_TOKEN="your-docat-api-token"
# Optional: set explicitly, or let the workflow infer it.
# DOCAT_VERSION="0.3.0"
```

Load `.env` and resolve `DOCAT_VERSION`:

```bash
set -a
source .env
set +a

# Option A (default): infer from pyproject.toml if not provided.
if [ -z "${DOCAT_VERSION:-}" ]; then
  DOCAT_VERSION="$(uv run python -c 'import tomllib; from pathlib import Path; print(tomllib.loads(Path("pyproject.toml").read_text())["project"]["version"])')"
fi

# Option B (release/tag flow): prefer exact git tag on release commits.
# uv-ship uses tag prefix "v", so this strips the prefix (e.g., v0.3.0 -> 0.3.0).
if git describe --tags --exact-match >/dev/null 2>&1; then
  DOCAT_VERSION="$(git describe --tags --exact-match | sed 's/^v//')"
fi
```

Upload the artifact:

```bash
curl -X POST -F "file=@docs-dist/ai-vfs-docs.zip" \
  "${DOCAT_HOST}/api/${DOCAT_PROJECT}/${DOCAT_VERSION}"
```

If the project is claimed, include authentication:

```bash
curl -X POST \
  -H "Docat-Api-Key: ${DOCAT_TOKEN}" \
  -F "file=@docs-dist/ai-vfs-docs.zip" \
  "${DOCAT_HOST}/api/${DOCAT_PROJECT}/${DOCAT_VERSION}"
```

Optionally tag this version as `latest`:

```bash
curl -X PUT \
  -H "Docat-Api-Key: ${DOCAT_TOKEN}" \
  "${DOCAT_HOST}/api/${DOCAT_PROJECT}/${DOCAT_VERSION}/tags/latest"
```

## Verification checklist

- Confirm artifact exists at `docs-dist/ai-vfs-docs.zip`.
- Confirm exactly one upload artifact is produced for each build run.
- Confirm `DOCAT_VERSION` resolved to the intended value (from `.env`, tag, or `pyproject.toml`).
- Confirm Docat serves the uploaded version at:
  - `${DOCAT_HOST}/${DOCAT_PROJECT}/${DOCAT_VERSION}/`
