"""Render advisory plan data as Markdown, without executing or trusting review text."""
import html
import re

from pardis_cloud_mcp.deployment import ResourcePreset, SourceAssessment, Target


def text(value: str) -> str:
    value = " ".join(value.split())
    return re.sub(r"([\\`*_{}\[\]()#+.!|~-])", r"\\\1", html.escape(value))


def render_document(name: str, assessment: SourceAssessment, target: Target, preset: ResourcePreset) -> str:
    resources = preset.resources
    review = "Ready for planning" if assessment.readiness == "ready_for_planning" else "More information is needed"
    lines = [f"# Deployment plan: {text(name)}", "",
             "**Draft proposal — no resources have been created. Deployment is not available yet.**", "",
             "## Application review", "", f"- Review result: {review}.",
             f"- Application type: {text(assessment.stack)}.",
             f"- Source label: {text(assessment.source.repository_id)}.",
             f"- Source revision: {text(assessment.source.revision) if assessment.source.revision else 'Not yet pinned; required before deployment'}.",
             f"- Build observation: {text(assessment.build_observation)}",
             f"- Startup observation: {text(assessment.start_observation)}", "",
             "This review was supplied by the agent from the source it could access. MCP has not independently verified or executed the source.", "",
             "## Proposed hosting", "", f"Candidate: **{target.upper()}**."]
    lines += (["Use a container-based application deployment as the starting proposal. Container readiness and available CCI features still need verification."]
              if target == "cci" else
              ["Use a virtual machine as the starting proposal. An approved machine image, network and application installation method still need selection; a running VM alone is not a working application."])
    lines += ["", "### Initial sizing proposal", "",
              f"- CPU: {resources.cpu_millicores / 1000:g} vCPU per instance.",
              f"- Memory: {resources.memory_mib} MiB per instance.",
              f"- Instances: {resources.replicas}.",
              f"- Disk: {resources.disk_gib} GiB requested per instance.",
              "These are starting estimates, not verified capacity, available flavors or a performance guarantee.", "",
              "## Other services", ""]
    for dependency in assessment.dependencies:
        lines.append(f"- {text(dependency.name)} ({text(dependency.kind)}): {text(dependency.purpose)}. Select a compatible service and verify access before execution.")
    if not assessment.dependencies:
        lines.append("No additional dependencies were identified in the submitted review. Confirm this against the complete application.")
    lines += ["", "## Network and configuration", "",
              "- Start with private access. Public exposure requires a separate reviewed plan and approval.",
              f"- Application port: {assessment.port if assessment.port is not None else 'not identified'}.",
              f"- Health endpoint: {text(assessment.health_path) if assessment.health_path else 'not identified'}.",
              "- Network, project placement, security rules and service compatibility are not verified yet."]
    for env in assessment.environment:
        lines.append(f"- {text(env.name)}: {env.kind}; {'required' if env.required else 'optional'}"
                     + (f"; reference {text(env.secret_ref)}" if env.secret_ref else "") + ". Never put secret values in this document.")
    lines += ["", "## Review findings", ""]
    for finding in assessment.findings:
        lines.append(f"- {finding.severity.upper()} ({finding.provenance}): {text(finding.message)}")
        for evidence in finding.evidence:
            lines.append(f"  - Evidence: {text(evidence.file)}:{evidence.line} — {text(evidence.observation)}")
    if not assessment.findings:
        lines.append("No findings were supplied; this is not a security-review certification.")
    lines += ["", "## Before deployment", ""]
    lines.extend(f"- {text(item)}" for item in assessment.missing_inputs)
    if assessment.stack == "unknown":
        lines.append("- Identify the application stack and an approved build recipe.")
    if assessment.port is None or assessment.health_path is None:
        lines.append("- Identify the application port and implement a health check.")
    if any(env.required and env.kind == "secret" and not env.secret_ref for env in assessment.environment):
        lines.append("- Configure the missing secret references through the approved secret store.")
    lines += ["- Resolve all blocker findings; pin and verify the source and resulting application image.",
              "- Verify actual Pardis permissions, network and capacity for every selected service.",
              "- Choose the build/delivery workflow and review the final configuration.", "",
              "## Cost, quota and approval", "",
              "Cost: **unknown**, not zero. Quota: **not checked**. Approval: **not requested**.",
              "Your role permits preparing this document; it does not prove permission to deploy its resources.", "",
              "## Proposed delivery steps", "",
              "1. Resolve the outstanding questions and review the final plan.",
              "2. Approve the exact configuration after access, cost and quota checks.",
              "3. Build the pinned source in an isolated environment and record the image digest.",
              "4. Create only the approved resources and deploy the application.",
              "5. Verify application health, then return the reachable endpoint and logs.", "",
              "## Failure and cleanup", "",
              "Record created resources and failed stages. Do not repeat an uncertain creation request or delete shared resources automatically. Agree rollback and cleanup ownership before execution.", "",
              "## Next action", "",
              "Review this proposal and resolve the open questions. You can request a revised plan. Saying 'deploy it' cannot start deployment in this version.", ""]
    return "\n".join(lines)
