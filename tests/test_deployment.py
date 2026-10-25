import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from pardis_cloud_mcp.config import Config
from pardis_cloud_mcp.deployment import ApplicationBinding, DeploymentPlan, RolePolicy, SourceAssessment


EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "deployment"


def read_example(name):
    return json.loads((EXAMPLES / name).read_text())


@pytest.mark.parametrize("model,name", [
    (SourceAssessment, "assessment.json"),
    (SourceAssessment, "assessment.needs-input.json"),
    (ApplicationBinding, "application.json"),
    (RolePolicy, "role-policy.json"),
    (DeploymentPlan, "plan.cci.json"),
    (DeploymentPlan, "plan.ecs.json"),
])
def test_example_contract_round_trip(model, name):
    document = model.model_validate(read_example(name))
    assert model.model_validate_json(document.model_dump_json()) == document


@pytest.mark.parametrize("path,value", [
    (("source", "revision"), "main"),
    (("source", "repository_id"), "https://untrusted.example/repository"),
    (("port",), 0), (("port",), 65536), (("port",), "8080"), (("port",), True),
    (("health_path",), "https://untrusted.example/health"),
    (("stack",), "unknown-stack"),
    (("build_observation",), "x" * 2001),
    (("findings",), [{}] * 65),
    (("environment",), [{"name": "PASSWORD", "kind": "secret", "value": "DO-NOT-ECHO"}]),
    (("environment",), [{"name": "URL", "kind": "config", "secret_ref": "database-url"}]),
    (("Department",), "admin"), (("Team",), "admin"),
    (("pardis_roles",), ["admin"]), (("owner",), {"subject": "another-user"}),
    (("access_token",), "DO-NOT-ECHO"),
])
def test_assessment_rejects_invalid_and_identity_inputs(path, value):
    payload = read_example("assessment.json")
    parent = payload
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = value
    with pytest.raises(ValidationError):
        SourceAssessment.model_validate(payload)


def test_unknown_revision_is_allowed_for_preview_but_false_readiness_is_rejected():
    payload = read_example("assessment.json")
    del payload["source"]["revision"]
    assert SourceAssessment.model_validate(payload).source.revision is None
    payload = read_example("assessment.needs-input.json")
    assert SourceAssessment.model_validate(payload).readiness == "needs_input"
    payload["readiness"] = "ready_for_planning"
    with pytest.raises(ValidationError):
        SourceAssessment.model_validate(payload)


@pytest.mark.parametrize("path", ["/etc/passwd", "../app.py", "a/../../app.py", "a\\app.py", "a//app.py", "a\n.py"])
def test_evidence_paths_cannot_escape_repository(path):
    payload = read_example("assessment.json")
    payload["findings"][0]["evidence"][0]["file"] = path
    with pytest.raises(ValidationError):
        SourceAssessment.model_validate(payload)


def test_observed_findings_require_evidence_and_env_names_are_unique():
    payload = read_example("assessment.json")
    payload["findings"][0]["evidence"] = []
    with pytest.raises(ValidationError):
        SourceAssessment.model_validate(payload)
    payload = read_example("assessment.json")
    payload["environment"] = [{"name": "PORT", "kind": "config"}] * 2
    with pytest.raises(ValidationError):
        SourceAssessment.model_validate(payload)


@pytest.mark.parametrize("field,value", [
    ("cpu_millicores", 0), ("cpu_millicores", 64001), ("memory_mib", -1),
    ("disk_gib", 1025), ("replicas", 21), ("replicas", "1"),
])
def test_resource_limits_are_validated(field, value):
    payload = read_example("plan.cci.json")
    payload["preset"]["resources"][field] = value
    with pytest.raises(ValidationError):
        DeploymentPlan.model_validate(payload)


@pytest.mark.parametrize("field,value", [
    ("executable", True), ("status", "approved"), ("approval_status", "approved"),
    ("cost_status", "free"), ("quota_status", "passed"), ("unresolved", []),
    ("command", "echo must-not-execute"),
])
def test_draft_cannot_claim_execution_readiness(field, value):
    payload = read_example("plan.cci.json")
    payload[field] = value
    with pytest.raises(ValidationError):
        DeploymentPlan.model_validate(payload)


@pytest.mark.parametrize("field", ["source_repository_id", "target_binding_id"])
def test_application_must_match_plan(field):
    payload = read_example("plan.ecs.json")
    payload["application"][field] = "other-application"
    with pytest.raises(ValidationError):
        DeploymentPlan.model_validate(payload)


@pytest.mark.parametrize("issuer", ["https://user:DO-NOT-ECHO@identity.example.com", "https:///realm", "http://identity.example.com"])
def test_internal_actor_issuer_rejects_embedded_credentials_and_invalid_urls(issuer):
    payload = read_example("application.json")
    payload["owner"]["issuer"] = issuer
    with pytest.raises(ValidationError):
        ApplicationBinding.model_validate(payload)


def test_plan_defaults_remain_draft_and_unknown():
    for name in ("plan.ecs.json", "plan.cci.json"):
        plan = DeploymentPlan.model_validate(read_example(name))
        assert plan.executable is False
        assert plan.status == "draft"
        assert plan.cost_status == plan.quota_status == "unknown"
        assert plan.approval_status == "not_requested"
        assert plan.application.shared_roles == []


@pytest.mark.parametrize("case", ["missing-binding", "missing-preset", "duplicate-role", "duplicate-preset",
                                 "cci-no-namespace", "ecs-with-namespace", "attribute-rule", "unknown-action"])
def test_role_policy_references_and_boundaries(case):
    payload = read_example("role-policy.json")
    if case == "missing-binding":
        payload["grants"][0]["target_bindings"] = ["missing"]
    elif case == "missing-preset":
        payload["grants"][0]["presets"] = ["missing"]
    elif case == "duplicate-role":
        payload["grants"].append(payload["grants"][0])
    elif case == "duplicate-preset":
        payload["presets"].append(payload["presets"][0])
    elif case == "cci-no-namespace":
        del payload["target_bindings"][0]["cci_namespace"]
    elif case == "ecs-with-namespace":
        payload["target_bindings"][1]["cci_namespace"] = "demo"
    elif case == "attribute-rule":
        payload["grants"][0]["Department"] = "Tech"
    else:
        payload["grants"][0]["actions"] = ["shell"]
    with pytest.raises(ValidationError):
        RolePolicy.model_validate(payload)


def test_d0_policy_is_not_live_configuration():
    with pytest.raises(ValidationError):
        Config.model_validate({"tools": {"enabled": ["whoami"]}, "role_policy": read_example("role-policy.json")})


def validate(kind, path):
    return subprocess.run([sys.executable, str(EXAMPLES / "validate.py"), kind, str(path)],
                          capture_output=True, text=True, timeout=10)


def test_offline_validation_cli_and_safe_errors(tmp_path):
    result = validate("plan", EXAMPLES / "plan.cci.json")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["executable"] is False
    result = validate("assessment", EXAMPLES / "assessment.invalid.json")
    assert result.returncode == 1
    assert "source.revision" in result.stdout
    assert "environment.0.value" in result.stdout
    assert "SYNTHETIC-DO-NOT-ECHO" not in result.stdout + result.stderr
    for data in (b"x" * (128 * 1024 + 1), b"not-json", b"\xff"):
        path = tmp_path / "invalid.json"
        path.write_bytes(data)
        result = validate("assessment", path)
        assert result.returncode == 1
        assert "Traceback" not in result.stderr
    assert validate("assessment", tmp_path / "missing.json").returncode == 1


def test_fixture_health_handler_without_starting_server():
    spec = importlib.util.spec_from_file_location("sample_http", EXAMPLES / "python-http" / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from io import BytesIO
    from unittest.mock import Mock

    for path, status in (("/health", 200), ("/", 200), ("/missing", 404)):
        handler = Mock(path=path, wfile=BytesIO())
        module.Handler.do_GET(handler)
        if status == 200:
            handler.send_response.assert_called_once_with(200)
            assert json.loads(handler.wfile.getvalue())["status"] == "ok"
        else:
            handler.send_error.assert_called_once_with(404)
