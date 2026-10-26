"""Recheck ECS choices through user-scoped catalogs; never provision resources."""
import asyncio
from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import Field

from pardis_cloud_mcp.cloud import CloudError
from pardis_cloud_mcp.deployment import Contract, EnterpriseProjectId, ResourceSize
from pardis_cloud_mcp.discovery import DiscoveryQuery, EcsDiscovery, EcsOption, ResourceId


class EcsSelection(Contract):
    project_id: Annotated[str | None, Field(pattern=r"^[a-fA-F0-9]{32}$")] = None
    enterprise_project_id: EnterpriseProjectId
    vpc_id: ResourceId
    subnet_id: ResourceId
    security_group_id: ResourceId
    image_id: ResourceId
    image_type: Literal["gold", "private", "shared"] = "gold"
    flavor_id: ResourceId
    availability_zone: ResourceId
    root_disk_gib: int = Field(default=20, ge=10, le=1024)


class EcsPlanSelection(Contract):
    selection: EcsSelection
    enterprise_project: EcsOption
    vpc: EcsOption
    subnet: EcsOption
    security_group: EcsOption
    image: EcsOption
    flavor: EcsOption
    availability_zone: EcsOption
    resources: ResourceSize
    observed_at: str
    catalog_audit_ids: dict[str, str]
    checks: list[str]
    unknowns: list[str]
    write_access: Literal["not_checked"] = "not_checked"


class EcsPlanVerifier:
    def __init__(self, discovery: EcsDiscovery):
        self.discovery = discovery

    async def _find(self, token: str, resource_id: str, query: DiscoveryQuery):
        seen = set()
        # shortcut: bounded catalog scans reuse existing APIs; add detail lookups if real catalogs exceed this limit.
        for _ in range(10):
            page = await self.discovery.list(token, query)
            matches = [item for item in page.items if item.id == resource_id]
            if len(matches) > 1:
                raise CloudError("Cloud returned duplicate selection identifiers. Ask the operator to check API compatibility.")
            if matches:
                return matches[0], page
            cursor = (page.next_marker, page.next_offset)
            if cursor == (None, None):
                raise CloudError(f"The selected {query.resource_type} option is no longer visible with your access and filters. Discover it again.")
            if cursor in seen:
                raise CloudError("Cloud catalog pagination did not advance. No selection was saved.")
            seen.add(cursor)
            query = query.model_copy(update={"marker": page.next_marker, "offset": page.next_offset or 0})
        raise CloudError("Selection verification reached the catalog scan limit. Ask the operator to check the catalog size; no selection was saved.")

    @staticmethod
    def _number(option: EcsOption, field: str, *, required: bool = False) -> int | None:
        value = option.details.get(field)
        if value is None and not required:
            return None
        if type(value) is int and value >= 0:
            return value
        if isinstance(value, str) and value.isascii() and value.isdecimal():
            return int(value)
        raise CloudError("Cloud returned invalid sizing metadata for the selected image or flavor.")

    async def verify(self, token: str, selection: EcsSelection) -> EcsPlanSelection:
        try:
            async with asyncio.timeout(45):
                return await self._verify(token, selection)
        except TimeoutError:
            raise CloudError("ECS selection verification timed out. Try again; no selection was saved.") from None

    async def _verify(self, token: str, selection: EcsSelection) -> EcsPlanSelection:
        selected = {}
        audits = {}
        project_id = selection.project_id
        queries = (
            ("enterprise_project", "enterprise_projects", selection.enterprise_project_id, {}),
            ("vpc", "vpcs", selection.vpc_id, {}),
            ("subnet", "subnets", selection.subnet_id, {"vpc_id": selection.vpc_id}),
            ("security_group", "security_groups", selection.security_group_id, {}),
            ("availability_zone", "availability_zones", selection.availability_zone, {}),
            ("flavor", "flavors", selection.flavor_id, {"availability_zone": selection.availability_zone}),
            ("image", "images", selection.image_id, {"image_type": selection.image_type}),
        )
        for field, kind, resource_id, filters in queries:
            query = DiscoveryQuery(resource_type=kind, limit=100,
                                   project_id=None if kind == "enterprise_projects" else project_id, **filters)
            option, page = await self._find(token, resource_id, query)
            selected[field] = option
            audits[kind] = page.audit_id
            if kind != "enterprise_projects":
                if project_id is not None and page.project_id != project_id:
                    raise CloudError("Cloud discovery changed the selected regional project. Discover the options again.")
                project_id = page.project_id
        checks = ["All selected identifiers were found in fresh user-scoped catalogs.",
                  "The subnet belongs to the selected VPC.",
                  "The flavor was returned for the selected availability zone."]
        unknowns = ["Image/flavor CPU architecture, firmware and virtualization compatibility are not verified.",
                    "Security group rules, application reachability, disk type, capacity, pricing and quota are not verified.",
                    "Catalog visibility does not verify permission to deploy into the selected Enterprise Project."]
        group_vpc = selected["security_group"].details.get("vpc_id")
        if group_vpc and group_vpc != selection.vpc_id:
            raise CloudError("The selected security group belongs to a different VPC.")
        if group_vpc:
            checks.append("The security group belongs to the selected VPC.")
        else:
            unknowns.append("The security group catalog did not identify a VPC; its network compatibility is unverified.")
        if not selected["subnet"].details.get("neutron_network_id"):
            unknowns.append("The subnet catalog did not supply its ECS NIC network identifier.")
        for field, status in (("enterprise_project", 1), ("vpc", "OK"), ("subnet", "ACTIVE"), ("image", "active")):
            actual = selected[field].details.get("status")
            if actual is None:
                unknowns.append(f"The {field.replace('_', ' ')} catalog did not supply its operational status.")
            elif type(actual) is not type(status) or actual != status:
                raise CloudError(f"The selected {field.replace('_', ' ')} is not in a usable status.")
        disabled = selected["flavor"].details.get("OS-FLV-DISABLED:disabled")
        if disabled is not None and type(disabled) is not bool:
            raise CloudError("Cloud returned invalid flavor availability metadata.")
        if disabled is True:
            raise CloudError("The selected flavor is disabled.")
        image_type = selected["image"].details.get("__imagetype")
        if image_type is not None and image_type != selection.image_type:
            raise CloudError("The selected image does not match the requested image category.")
        cpu = self._number(selected["flavor"], "vcpus", required=True)
        memory = self._number(selected["flavor"], "ram", required=True)
        if not (1 <= cpu <= 64 and 128 <= memory <= 262144):
            raise CloudError("The selected flavor is outside the current planning bounds (1–64 vCPU, 128–262144 MiB RAM).")
        for field, available in (("min_disk", selection.root_disk_gib), ("min_ram", memory)):
            minimum = self._number(selected["image"], field)
            if minimum is None:
                unknowns.append(f"The image did not supply {field}; this sizing check is unverified.")
            elif available < minimum:
                raise CloudError("The selected disk or flavor RAM is below the image's minimum requirement.")
            else:
                checks.append(f"Selected sizing meets the image's {field} requirement.")
        return EcsPlanSelection(
            selection=selection.model_copy(update={"project_id": project_id}), **selected,
            resources=ResourceSize(cpu_millicores=cpu * 1000, memory_mib=memory,
                                   disk_gib=selection.root_disk_gib, replicas=1),
            observed_at=datetime.now(timezone.utc).isoformat(), catalog_audit_ids=audits,
            checks=checks, unknowns=unknowns,
        )
