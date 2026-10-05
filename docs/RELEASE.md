# Automated images and semantic releases

`.github/workflows/release.yml` follows the semantic-release pattern used by
[keycloak-email-otp-authenticator](https://github.com/ehsanmsb/keycloak-email-otp-authenticator/blob/main/.github/workflows/release.yml)
and the GHCR image publication pattern used by
[mkdocs-mcp](https://github.com/ehsanmsb/mkdocs-mcp/blob/main/.github/workflows/release.yml).
Release rules are in `.releaserc.yml`.

## What happens

1. Pull requests to `main` and pushes to `main` run the Python tests, including the isolated MCP Helm
   template tests. The Redis chart is not downloaded or checked by these tests.
2. Pull requests also check the Docker build. They do not publish images or releases.
3. After tests pass on `main`, CI builds a `linux/amd64` image and pushes `main` and
   `sha-<full-commit-sha>` tags to `ghcr.io/ehsanmsb/pardis-cloud-mcp`.
4. semantic-release examines the commits since the last `vX.Y.Z` Git tag. When a release is needed,
   it gives that same image the new version, `<major>.<minor>` and `latest` tags, then creates the Git
   tag and a GitHub release with generated notes. It uses the build's digest, so the version tags point
   to the image built in that workflow run without a second build.

Image publication runs before the Git tag is created. A failed build or registry promotion stops release
creation. As with any multi-service publication, a later GitHub failure can leave image tags already
published; inspect the workflow and existing Git tags/releases before retrying a partial release.

Every successful main-branch run publishes a commit image even if semantic-release finds no new release.
Runs are serialized with `cancel-in-progress: false`; GitHub concurrency can replace older pending runs
when pushes arrive faster than builds complete. The workflow also supports **Run workflow** on `main`.
Manual runs on other branches cannot publish.

## Commit messages and versions

| Commit example | Release |
| --- | --- |
| `fix: reject expired cloud credentials` | Patch, e.g. `1.2.3` → `1.2.4` |
| `perf: reduce Redis round trips` | Patch |
| `feat: add ECS creation tool` | Minor, e.g. `1.2.3` → `1.3.0` |
| `feat!: change the tool configuration format` | Major, e.g. `1.2.3` → `2.0.0` |
| Any commit with a `BREAKING CHANGE:` footer | Major |
| `docs: ...`, `test: ...`, `chore: ...`, `ci: ...`, `build: ...`, `refactor: ...` without a breaking change | Commit image only |

The highest applicable change wins when several commits are included. Squash merges must retain a
Conventional Commit title, since that becomes the commit analyzed on `main`. If a dependency or build
change needs a patch release, use a suitable `fix:` commit rather than `chore:` or `build:`.

Without an existing matching release tag, semantic-release starts at **1.0.0** when it finds a releasable
change. It does not take the starting version from `pyproject.toml`. Existing historical `feat:` commits
can therefore trigger the first release when this workflow is introduced.

This automation versions Git releases and container tags. It does not rewrite `pyproject.toml`,
`uv.lock` or Helm `Chart.yaml`, publish a Python package or Helm chart, or commit version bumps back
to `main`. The Python package's development metadata is separate from the container release version.
Git source/revision OCI labels identify the code included in each image.

## GitHub setup

Enable GitHub Actions in the repository and allow the workflow to obtain `contents: write` and
`packages: write`. The workflow uses the automatically provided `GITHUB_TOKEN`; no Docker Hub
credentials, cloud credentials, Keycloak secrets or Redis secrets are needed to build or publish.
It does not post comments on issues or pull requests.

If a GHCR package with this name already exists, its Actions access must allow this repository to push.
Package visibility is managed separately from the repository: for anonymous cluster pulls, make the
package public after its first publication; otherwise configure Kubernetes `imagePullSecrets`.
See [GitHub's GHCR authentication documentation](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry#authenticating-to-the-container-registry).

Repository or organization rules restricting tag creation can prevent semantic-release from creating
`v*` tags. Configure the intended release workflow's access according to your repository policy.
The workflow creates no automatic branch commits; the configured Git identity for release operations
is `ehsanmsb <ehsan.mosaieby@gmail.com>`.

## Use an image

For a published release such as `1.2.3`, available tags are:

```text
ghcr.io/ehsanmsb/pardis-cloud-mcp:1.2.3
ghcr.io/ehsanmsb/pardis-cloud-mcp:1.2
ghcr.io/ehsanmsb/pardis-cloud-mcp:latest
ghcr.io/ehsanmsb/pardis-cloud-mcp:main
ghcr.io/ehsanmsb/pardis-cloud-mcp:sha-<full-commit-sha>
```

`latest` tracks the latest semantic release; `main` also moves for non-release commits. Re-running a
commit build can update its `sha-...` tag, so deploy by registry digest if you need immutable image content.

Select an existing version in Helm values:

```yaml
image:
  repository: ghcr.io/ehsanmsb/pardis-cloud-mcp
  tag: "1.2.3"
```

Then follow [the Helm deployment guide](HELM.md). The workflow publishes images; it does not upgrade
your cluster or change its selected image version automatically.

## Check the workflow

Before pushing, run `uv run --locked --extra dev pytest -q`. Release tests check the registry-promotion
command with a fake Docker executable, including failure handling, and check the workflow's publishing
boundary. They do not publish anything. If installed, `actionlint .github/workflows/release.yml` also
validates GitHub Actions syntax.

After pushing to `main`, inspect **Actions → Build and Release**, then check the GHCR tags and GitHub
release. Test the published image by deploying a selected version to your test namespace and calling
`whoami` after browser login. A local test pass does not establish that GitHub's package/tag permissions
or the first remote build have succeeded.

Reference: [semantic-release configuration](https://semantic-release.org/usage/configuration/).
