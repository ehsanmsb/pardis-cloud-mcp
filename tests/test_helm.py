"""Render only the MCP chart templates; no Redis download or cluster access."""
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

from pardis_cloud_mcp.config import Config


HELM = shutil.which("helm")
pytestmark = pytest.mark.skipif(HELM is None, reason="Helm CLI is not installed")


@pytest.fixture
def chart(tmp_path):
    source = Path(__file__).resolve().parents[1] / "helm"
    target = tmp_path / "chart"
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("charts", "values.local.yaml"))
    metadata = yaml.safe_load((target / "Chart.yaml").read_text())
    # Test our templates in isolation; Redis is supplied by the separately managed dependency.
    metadata.pop("dependencies")
    (target / "Chart.yaml").write_text(yaml.safe_dump(metadata))
    return target


def render(chart, overrides=None):
    values = chart.parent / "overrides.yaml"
    values.write_text(yaml.safe_dump(overrides or {}))
    result = subprocess.run(
        [HELM, "template", "pardis-test", str(chart), "--namespace", "mcp-test", "-f", str(values)],
        capture_output=True, text=True, check=True,
    )
    return {item["kind"]: item for item in yaml.safe_load_all(result.stdout) if item}


def test_default_templates_and_secret_references(chart):
    subprocess.run([HELM, "lint", str(chart), "--strict"], capture_output=True, text=True, check=True)
    docs = render(chart)
    assert set(docs) == {"Deployment", "Service", "ConfigMap", "HTTPProxy"}
    pod = docs["Deployment"]["spec"]["template"]
    container = pod["spec"]["containers"][0]
    config = Config.model_validate(yaml.safe_load(docs["ConfigMap"]["data"]["config.yaml"]))
    assert config.tools.enabled == ["whoami"]
    assert config.tools.disabled == []
    assert container["envFrom"] == [{"secretRef": {"name": "snapp-support-mcp"}}]
    env = {item["name"]: item["value"] for item in container["env"]}
    assert env["MCP_HOST"] == "0.0.0.0"
    assert not {"OIDC_CLIENT_SECRET", "TOKEN_ENCRYPTION_KEY", "REDIS_URL", "REDIS_PASSWORD"} & env.keys()
    assert container["volumeMounts"][0]["mountPath"] == env["MCP_CONFIG_FILE"]
    assert container["volumeMounts"][0]["readOnly"] is True
    assert pod["spec"]["volumes"][0]["configMap"]["name"] == docs["ConfigMap"]["metadata"]["name"]
    assert container["readinessProbe"]["httpGet"]["path"] == "/health"
    assert "tcpSocket" in container["livenessProbe"]
    assert docs["Service"]["spec"]["selector"] == docs["Deployment"]["spec"]["selector"]["matchLabels"]
    assert docs["Service"]["spec"]["selector"].items() <= pod["metadata"]["labels"].items()
    proxy = docs["HTTPProxy"]["spec"]
    assert proxy["ingressClassName"] == "private"
    assert proxy["virtualhost"]["tls"]["enableFallbackCertificate"] is False
    assert proxy["virtualhost"]["tls"]["secretName"] == "example-certificates/exxample-com-tls"
    assert proxy["routes"][0]["conditions"] == [{"prefix": "/"}]
    assert proxy["routes"][0]["services"] == [{"name": docs["Service"]["metadata"]["name"], "port": 8000}]


def test_config_changes_trigger_rollout_and_env_is_customizable(chart):
    original = render(chart)
    changed = render(chart, {
        "config": {"tools": {"enabled": ["whoami", "list_ecs"], "disabled": ["list_ecs"]}},
        "env": {"EXTRA_SETTING": "custom", "MCP_PORT": "9000", "PARDIS_PROJECT_ID": None,
                "MCP_CONFIG_FILE": "/etc/mcp/config.yaml"},
        "service": {"port": 9001},
        "image": {"repository": "registry.example.org/mcp", "tag": "test"},
        "httpProxy": {"virtualhost": {"fqdn": "mcp.example.org"}},
    })
    before = original["Deployment"]["spec"]["template"]
    after = changed["Deployment"]["spec"]["template"]
    assert before["metadata"]["annotations"]["checksum/config"] != after["metadata"]["annotations"]["checksum/config"]
    tools = yaml.safe_load(changed["ConfigMap"]["data"]["config.yaml"])["tools"]
    assert tools == {"enabled": ["whoami", "list_ecs"], "disabled": ["list_ecs"]}
    container = after["spec"]["containers"][0]
    env = {item["name"]: item["value"] for item in container["env"]}
    assert env["EXTRA_SETTING"] == "custom"
    assert "PARDIS_PROJECT_ID" not in env
    assert container["image"] == "registry.example.org/mcp:test"
    assert container["ports"][0]["containerPort"] == 9000
    assert container["volumeMounts"][0]["mountPath"] == "/etc/mcp/config.yaml"
    assert changed["Service"]["spec"]["ports"][0] == {"name": "http", "port": 9001, "targetPort": "http"}
    assert changed["HTTPProxy"]["spec"]["routes"][0]["services"][0]["port"] == 9001
    assert changed["HTTPProxy"]["spec"]["virtualhost"]["fqdn"] == "mcp.example.org"


def test_external_redis_without_httpproxy(chart):
    docs = render(chart, {
        "redis": {"enabled": False}, "httpProxy": {"enabled": False},
        "envFrom": [{"secretRef": {"name": "external-mcp-secrets"}}],
        "config": {"tools": {"enabled": [], "disabled": []}},
    })
    assert "HTTPProxy" not in docs
    assert docs["Deployment"]["spec"]["template"]["spec"]["containers"][0]["envFrom"] == [
        {"secretRef": {"name": "external-mcp-secrets"}},
    ]
    assert yaml.safe_load(docs["ConfigMap"]["data"]["config.yaml"])["tools"]["enabled"] == []
