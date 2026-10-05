"""Release command checks with no real registry or GitHub publication."""
import os
from pathlib import Path
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]


def release_config():
    return yaml.safe_load((ROOT / ".releaserc.yml").read_text())


def test_only_main_can_publish_and_build_precedes_release():
    # BaseLoader preserves the GitHub Actions `on` key instead of YAML 1.1's boolean conversion.
    workflow = yaml.load((ROOT / ".github/workflows/release.yml").read_text(), Loader=yaml.BaseLoader)
    assert workflow["on"]["push"]["branches"] == ["main"]
    assert "pull_request_target" not in workflow["on"]
    assert workflow["permissions"] == {"contents": "read"}
    release = workflow["jobs"]["release"]
    assert release["needs"] == "test"
    assert release["if"] == "github.ref == 'refs/heads/main' && github.event_name != 'pull_request'"
    steps = release["steps"]
    build_index = next(i for i, step in enumerate(steps) if step.get("id") == "image")
    publish_index = next(i for i, step in enumerate(steps) if "semantic-release\n" in step.get("run", ""))
    assert build_index < publish_index
    assert steps[build_index]["with"]["push"] == "true"
    assert "${{ github.sha }}" in steps[build_index]["with"]["tags"]
    assert steps[publish_index]["env"]["IMAGE_DIGEST"] == "${{ steps.image.outputs.digest }}"
    config = release_config()
    assert config["branches"] == ["main"]
    plugins = dict(config["plugins"])
    assert "@semantic-release/npm" not in plugins
    assert plugins["@semantic-release/github"]["successComment"] is False
    assert plugins["@semantic-release/github"]["failComment"] is False


@pytest.mark.parametrize("version,exit_code", [("1.2.3", 0), ("2.0.0", 0), ("1.2.4", 17)])
def test_promotes_the_built_digest_and_propagates_registry_failure(tmp_path, version, exit_code):
    command = dict(release_config()["plugins"])["@semantic-release/exec"]["prepareCmd"]
    command = command.replace("${nextRelease.version}", version)
    docker = tmp_path / "docker"
    docker.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$DOCKER_ARGUMENTS"\nexit "$DOCKER_EXIT_CODE"\n')
    docker.chmod(0o755)
    arguments = tmp_path / "arguments.txt"
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}",
           "DOCKER_ARGUMENTS": str(arguments), "DOCKER_EXIT_CODE": str(exit_code),
           "IMAGE_NAME": "ghcr.io/example/mcp", "IMAGE_DIGEST": "sha256:" + "a" * 64}
    result = subprocess.run(["/bin/sh", "-c", command], env=env, capture_output=True, text=True)
    assert result.returncode == exit_code, result.stderr
    assert arguments.read_text().splitlines() == [
        "buildx", "imagetools", "create",
        "--tag", f"ghcr.io/example/mcp:{version}",
        "--tag", f"ghcr.io/example/mcp:{'.'.join(version.split('.')[:2])}",
        "--tag", "ghcr.io/example/mcp:latest",
        "ghcr.io/example/mcp@sha256:" + "a" * 64,
    ]
