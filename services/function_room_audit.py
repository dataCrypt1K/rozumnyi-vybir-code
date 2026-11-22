from __future__ import annotations

from typing import Literal, Mapping

from pydantic import Field

from domain.enums import LocationKind, PropertyType, RoomType
from domain.models import DomainModel
from services.function_catalog import FunctionCatalog
from services.function_rules import FunctionRulesConfig, TargetRole
from services.function_scope import FUNCTION_ZONE_LIMITS
from services.object_step_service import LOCATION_TYPES


AUDIT_SCHEMA_VERSION = "function_room_audit_v2"
AuditStatus = Literal["ok", "review", "warning"]
ReviewPriority = Literal["none", "low", "medium", "high"]


AUDIT_NOTES_UK = {
    "MISSING_TARGET": "Функція не має жодної конкретної або умовної локації.",
    "GENERIC_OTHER_TARGET": (
        "До переліку входить узагальнене приміщення «Інше»; потрібно перевірити, "
        "чи не робить воно правило надто широким."
    ),
    "VIRTUAL_LOCATION_TARGET": (
        "Є віртуальна технічна локація без прямого типу приміщення; точне місце "
        "потрібно уточнювати для конкретного об'єкта."
    ),
    "BROAD_TARGET_SCOPE": (
        "Функція має щонайменше 12 потенційних зон. Це не помилка, але кінцевий "
        "обсяг потрібно перевіряти предметними сценаріями."
    ),
    "PROPERTY_TARGET_MISMATCH": (
        "Для одного із заявлених типів житла немає жодної доступної локації."
    ),
}


class RoomAuditColumn(DomainModel):
    room_type: RoomType
    name_uk: str
    kind: LocationKind
    property_types: list[PropertyType]


class FunctionRoomAuditRow(DomainModel):
    function_id: str
    system_id: str
    system_name_uk: str
    function_name_uk: str
    property_types: list[PropertyType]
    room_aliases: list[str] = Field(default_factory=list)
    location_aliases: list[str] = Field(default_factory=list)
    allowed_room_types: list[RoomType] = Field(default_factory=list)
    conditional_room_types: list[RoomType] = Field(default_factory=list)
    service_room_types: list[RoomType] = Field(default_factory=list)
    endpoint_room_types: list[RoomType] = Field(default_factory=list)
    infrastructure_room_types: list[RoomType] = Field(default_factory=list)
    aggregate_room_types: list[RoomType] = Field(default_factory=list)
    virtual_aliases: list[str] = Field(default_factory=list)
    selection_unit: str
    zone_limit: int | None = None
    audit_status: AuditStatus
    review_priority: ReviewPriority
    audit_codes: list[str] = Field(default_factory=list)
    audit_notes_uk: list[str] = Field(default_factory=list)


class FunctionRoomAuditReport(DomainModel):
    schema_version: str = AUDIT_SCHEMA_VERSION
    function_catalog_version: str
    function_rules_version: str
    room_columns: list[RoomAuditColumn]
    functions: list[FunctionRoomAuditRow]


def _ordered_room_types(values: set[RoomType]) -> list[RoomType]:
    return [item.room_type for item in LOCATION_TYPES if item.room_type in values]


def _property_has_target(
    property_type: PropertyType,
    room_types: set[RoomType],
) -> bool:
    return any(
        item.room_type in room_types and property_type in item.property_types
        for item in LOCATION_TYPES
    )


def build_function_room_audit(
    catalog: FunctionCatalog,
    rules: FunctionRulesConfig,
    *,
    system_names: Mapping[str, str] | None = None,
) -> FunctionRoomAuditReport:
    names = system_names or {}
    room_columns = [
        RoomAuditColumn(
            room_type=item.room_type,
            name_uk=item.name_uk,
            kind=item.kind,
            property_types=sorted(item.property_types, key=lambda value: value.value),
        )
        for item in LOCATION_TYPES
    ]
    rows: list[FunctionRoomAuditRow] = []

    for function in catalog.functions:
        targets_by_role: dict[TargetRole, set[RoomType]] = {
            "service_zone": set(),
            "device_endpoint": set(),
            "infrastructure_anchor": set(),
            "aggregate_scope": set(),
        }
        virtual_aliases: list[str] = []

        for alias in function.applicable_room_types:
            definition = rules.aliases.room_types[alias]
            if definition.resolution == "virtual":
                virtual_aliases.append(alias)
            else:
                targets_by_role[definition.target_role].update(
                    definition.ui_room_types
                )

        for alias in function.applicable_location_types:
            definition = rules.aliases.location_types[alias]
            if definition.resolution == "virtual":
                virtual_aliases.append(alias)
            else:
                targets_by_role[definition.target_role].update(
                    definition.ui_room_types
                )

        service_targets = targets_by_role["service_zone"]
        endpoint_targets = targets_by_role["device_endpoint"]
        infrastructure_targets = targets_by_role["infrastructure_anchor"]
        aggregate_targets = targets_by_role["aggregate_scope"]
        allowed = set().union(*targets_by_role.values())
        codes: list[str] = []
        if not allowed and not virtual_aliases:
            codes.append("MISSING_TARGET")
        if RoomType.OTHER in service_targets:
            codes.append("GENERIC_OTHER_TARGET")
        if virtual_aliases:
            codes.append("VIRTUAL_LOCATION_TARGET")
        if len(service_targets | endpoint_targets) >= 12:
            codes.append("BROAD_TARGET_SCOPE")

        if any(
            not _property_has_target(property_type, allowed)
            for property_type in function.applicable_property_types
        ):
            codes.append("PROPERTY_TARGET_MISMATCH")

        warning_codes = {"MISSING_TARGET", "PROPERTY_TARGET_MISMATCH"}
        if warning_codes.intersection(codes):
            audit_status: AuditStatus = "warning"
        elif codes:
            audit_status = "review"
        else:
            audit_status = "ok"

        high_priority_codes = {
            "MISSING_TARGET",
            "PROPERTY_TARGET_MISMATCH",
        }
        medium_priority_codes = {
            "GENERIC_OTHER_TARGET",
            "BROAD_TARGET_SCOPE",
        }
        if high_priority_codes.intersection(codes):
            review_priority: ReviewPriority = "high"
        elif medium_priority_codes.intersection(codes):
            review_priority = "medium"
        elif codes:
            review_priority = "low"
        else:
            review_priority = "none"

        rows.append(
            FunctionRoomAuditRow(
                function_id=function.function_id,
                system_id=function.system_id,
                system_name_uk=names.get(function.system_id, function.system_id),
                function_name_uk=function.name_uk,
                property_types=function.applicable_property_types,
                room_aliases=function.applicable_room_types,
                location_aliases=function.applicable_location_types,
                allowed_room_types=_ordered_room_types(allowed),
                conditional_room_types=[],
                service_room_types=_ordered_room_types(service_targets),
                endpoint_room_types=_ordered_room_types(endpoint_targets),
                infrastructure_room_types=_ordered_room_types(
                    infrastructure_targets
                ),
                aggregate_room_types=_ordered_room_types(aggregate_targets),
                virtual_aliases=list(dict.fromkeys(virtual_aliases)),
                selection_unit=function.selection_unit,
                zone_limit=FUNCTION_ZONE_LIMITS.get(function.function_id),
                audit_status=audit_status,
                review_priority=review_priority,
                audit_codes=codes,
                audit_notes_uk=[AUDIT_NOTES_UK[code] for code in codes],
            )
        )

    return FunctionRoomAuditReport(
        function_catalog_version=catalog.schema_version,
        function_rules_version=rules.schema_version,
        room_columns=room_columns,
        functions=rows,
    )
