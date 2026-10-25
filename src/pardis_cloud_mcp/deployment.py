"""D0 data contracts only: no authorization decisions, source execution or cloud calls."""
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator


Identifier = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,62}$")]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
Target = Literal["cci", "ecs"]
Action = Literal["prepare", "deploy", "status", "logs"]
Stack = Literal["python-http"]
ProjectId = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{32}$")]
EnterpriseProjectId = Annotated[str, StringConstraints(
    pattern=r"^(?:0|[a-f0-9]{32}|[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})$",
)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class SourceReference(Contract):
    repository_id: Identifier
    revision: Annotated[str, StringConstraints(pattern=r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")]


class Evidence(Contract):
    file: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    line: int = Field(ge=1, le=1_000_000)
    observation: Text

    @field_validator("file")
    @classmethod
    def relative_file(cls, value: str) -> str:
        if (value.startswith("/") or "\\" in value or ":" in value
                or any(part in ("", ".", "..") for part in value.split("/"))
                or any(ord(char) < 32 for char in value)):
            raise ValueError("Evidence must use a relative repository file path")
        return value


class Finding(Contract):
    severity: Literal["info", "warning", "blocker"]
    provenance: Literal["observed", "inferred", "unknown"]
    message: Text
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def observed_evidence(self) -> Self:
        if self.provenance == "observed" and not self.evidence:
            raise ValueError("Observed findings require file evidence")
        return self


class EnvironmentRequirement(Contract):
    name: Annotated[str, StringConstraints(pattern=r"^[A-Z_][A-Z0-9_]{0,127}$")]
    kind: Literal["config", "secret"]
    required: bool = True
    secret_ref: Identifier | None = None

    @model_validator(mode="after")
    def reference_kind(self) -> Self:
        if self.secret_ref is not None and self.kind != "secret":
            raise ValueError("Only secret requirements can contain a secret reference")
        return self


class Dependency(Contract):
    name: Identifier
    kind: Literal["database", "cache", "storage", "worker", "external-service"]
    purpose: Text


class SourceAssessment(Contract):
    schema_version: Literal[1] = 1
    source: SourceReference
    stack: Stack
    # Observations only; never executable build or bootstrap instructions.
    build_observation: Text
    start_observation: Text
    port: int | None = Field(default=None, ge=1, le=65535)
    health_path: Annotated[str, StringConstraints(pattern=r"^/[A-Za-z0-9/_-]*$", max_length=256)] | None = None
    environment: list[EnvironmentRequirement] = Field(default_factory=list, max_length=64)
    dependencies: list[Dependency] = Field(default_factory=list, max_length=32)
    findings: list[Finding] = Field(default_factory=list, max_length=64)
    missing_inputs: list[Text] = Field(default_factory=list, max_length=32)
    readiness: Literal["ready_for_planning", "needs_input"]

    @model_validator(mode="after")
    def readiness_matches_findings(self) -> Self:
        blocked = (self.port is None or self.health_path is None or self.missing_inputs
                   or any(f.severity == "blocker" for f in self.findings)
                   or any(e.required and e.kind == "secret" and e.secret_ref is None for e in self.environment))
        if self.readiness == "ready_for_planning" and blocked:
            raise ValueError("Unresolved inputs or blockers require needs_input readiness")
        names = [item.name for item in self.environment]
        if len(names) != len(set(names)):
            raise ValueError("Environment requirement names must be unique")
        return self


class Actor(Contract):
    """Internal authenticated identity; never supplied by an application tool caller."""
    issuer: Annotated[str, StringConstraints(pattern=r"^https://[^\s?#]+$", max_length=512)]
    subject: Name

    @field_validator("issuer")
    @classmethod
    def issuer_has_no_credentials(cls, value: str) -> str:
        parsed = urlsplit(value)
        if not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise ValueError("Issuer must have a hostname and must not contain credentials")
        return value


class ApplicationBinding(Contract):
    application_id: Identifier
    owner: Actor
    source_repository_id: Identifier
    target_binding_id: Identifier
    shared_roles: list[Identifier] = Field(default_factory=list, max_length=32)


class ResourceSize(Contract):
    cpu_millicores: int = Field(ge=100, le=64000)
    memory_mib: int = Field(ge=128, le=262144)
    disk_gib: int = Field(ge=0, le=1024)
    replicas: int = Field(ge=1, le=20)


class ResourcePreset(Contract):
    name: Identifier
    resources: ResourceSize


class TargetBinding(Contract):
    id: Identifier
    target: Target
    project_id: ProjectId
    enterprise_project_id: EnterpriseProjectId
    cci_namespace: Identifier | None = None

    @model_validator(mode="after")
    def namespace_matches_target(self) -> Self:
        if (self.target == "cci") != (self.cci_namespace is not None):
            raise ValueError("A CCI binding requires a namespace; an ECS binding must omit it")
        return self


class RoleGrant(Contract):
    role: Identifier
    actions: list[Action] = Field(min_length=1, max_length=4)
    target_bindings: list[Identifier] = Field(min_length=1, max_length=32)
    presets: list[Identifier] = Field(min_length=1, max_length=16)
    stacks: list[Stack] = Field(min_length=1, max_length=16)
    exposure: list[Literal["private", "public"]] = Field(min_length=1, max_length=2)


class RolePolicy(Contract):
    schema_version: Literal[1] = 1
    version: Identifier
    target_bindings: list[TargetBinding] = Field(min_length=1, max_length=64)
    presets: list[ResourcePreset] = Field(min_length=1, max_length=16)
    grants: list[RoleGrant] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def references_exist(self) -> Self:
        bindings = [item.id for item in self.target_bindings]
        presets = [item.name for item in self.presets]
        roles = [item.role for item in self.grants]
        if any(len(names) != len(set(names)) for names in (bindings, presets, roles)):
            raise ValueError("Target bindings, presets and roles must have unique names")
        for grant in self.grants:
            if not set(grant.target_bindings) <= set(bindings) or not set(grant.presets) <= set(presets):
                raise ValueError("Role grants must reference declared target bindings and presets")
        return self


class PlanStep(Contract):
    name: Identifier
    description: Text


class DeploymentPlan(Contract):
    """Draft contract; D3 adds persistence, hashes/expiry and executable-plan validation."""
    schema_version: Literal[1] = 1
    status: Literal["draft"] = "draft"
    executable: Literal[False] = False
    application: ApplicationBinding
    assessment: SourceAssessment
    target: TargetBinding
    preset: ResourcePreset
    exposure: Literal["private", "public"]
    rationale: Text
    steps: list[PlanStep] = Field(min_length=1, max_length=32)
    # No cost or quota provider exists at D0; unknown must not become zero or passed.
    cost_status: Literal["unknown"] = "unknown"
    quota_status: Literal["unknown"] = "unknown"
    approval_status: Literal["not_requested"] = "not_requested"
    unresolved: list[Text] = Field(min_length=1, max_length=32)
    rollback_notes: Text

    @model_validator(mode="after")
    def application_matches_plan(self) -> Self:
        if self.application.source_repository_id != self.assessment.source.repository_id:
            raise ValueError("Application source must match the assessment repository")
        if self.application.target_binding_id != self.target.id:
            raise ValueError("Application target binding must match the plan target")
        names = [step.name for step in self.steps]
        if len(names) != len(set(names)):
            raise ValueError("Plan step names must be unique")
        return self
