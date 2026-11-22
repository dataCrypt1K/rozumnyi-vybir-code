from __future__ import annotations

from datetime import date
from pathlib import Path
import re
from typing import Literal

import yaml
from pydantic import Field, field_validator, model_validator

from domain.enums import ChannelType
from domain.models import DomainModel


CATALOG_SCHEMA_VERSION = "0.1"
PRODUCT_ID_PATTERN = r"^[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+$"
EXTERNAL_PREREQUISITE_PATTERN = r"^external:[a-z][a-z0-9_]*$"
TechnologyStack = Literal["i3", "knx"]
ProductRole = Literal[
    "controller",
    "input_module",
    "relay_actuator",
    "ac_dimmer",
    "pwm_dimmer",
    "dali_gateway",
    "bus_power_supply",
    "programming_interface",
]


class StackCatalogPolicy(DomainModel):
    mandatory_product_ids: list[str] = Field(min_length=1)
    external_prerequisites: list[str] = Field(default_factory=list)
    policy_status: Literal["expert_assumption"]
    policy_note_uk: str = Field(min_length=1, max_length=2000)

    @field_validator("mandatory_product_ids")
    @classmethod
    def mandatory_ids_are_unique_and_valid(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("mandatory_product_ids must not contain duplicates")
        if any(not re.fullmatch(PRODUCT_ID_PATTERN, item) for item in value):
            raise ValueError("mandatory_product_ids contains an invalid identifier")
        return value

    @field_validator("external_prerequisites")
    @classmethod
    def external_prerequisites_are_valid(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("external_prerequisites must not contain duplicates")
        if any(
            not re.fullmatch(EXTERNAL_PREREQUISITE_PATTERN, item) for item in value
        ):
            raise ValueError("invalid external prerequisite identifier")
        return value


class EquipmentCatalogItem(DomainModel):
    product_id: str = Field(pattern=PRODUCT_ID_PATTERN, max_length=160)
    manufacturer: str = Field(min_length=1, max_length=160)
    model: str = Field(min_length=1, max_length=160)
    technology_stack: TechnologyStack
    roles: list[ProductRole] = Field(min_length=1)
    mandatory: bool
    capabilities: dict[ChannelType, int] = Field(default_factory=dict)
    requires_product_ids: list[str] = Field(default_factory=list)
    external_prerequisites: list[str] = Field(default_factory=list)
    source_ref: str = Field(min_length=1, max_length=1000)
    source_checked_at: date
    source_claim_uk: str = Field(min_length=1, max_length=1000)
    price_status: Literal["unavailable"]
    final_engineering_required: Literal[True]

    @field_validator("roles")
    @classmethod
    def roles_are_unique(cls, value: list[ProductRole]) -> list[ProductRole]:
        if len(value) != len(set(value)):
            raise ValueError("product roles must not contain duplicates")
        return value

    @field_validator("capabilities")
    @classmethod
    def capabilities_are_positive(
        cls, value: dict[ChannelType, int]
    ) -> dict[ChannelType, int]:
        if any(amount <= 0 for amount in value.values()):
            raise ValueError("product capabilities must be positive")
        return value

    @field_validator("requires_product_ids")
    @classmethod
    def required_product_ids_are_valid(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("requires_product_ids must not contain duplicates")
        if any(not re.fullmatch(PRODUCT_ID_PATTERN, item) for item in value):
            raise ValueError("requires_product_ids contains an invalid identifier")
        return value

    @field_validator("external_prerequisites")
    @classmethod
    def product_external_prerequisites_are_valid(
        cls, value: list[str]
    ) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("external_prerequisites must not contain duplicates")
        if any(
            not re.fullmatch(EXTERNAL_PREREQUISITE_PATTERN, item) for item in value
        ):
            raise ValueError("invalid external prerequisite identifier")
        return value

    @field_validator("source_ref")
    @classmethod
    def source_must_be_https(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("source_ref must use https")
        return value

    @model_validator(mode="after")
    def selectable_product_must_add_capacity(self) -> "EquipmentCatalogItem":
        if not self.mandatory and not self.capabilities:
            raise ValueError("a selectable product must provide channel capacity")
        if self.product_id in self.requires_product_ids:
            raise ValueError("a product cannot require itself")
        return self


class EquipmentCatalog(DomainModel):
    schema_version: str = Field(min_length=1, max_length=20)
    catalog_id: Literal["equipment_catalog_v1"]
    review_status: Literal["draft_for_expert_review", "expert_reviewed"]
    source_checked_at: date
    scope_note_uk: str = Field(min_length=1, max_length=3000)
    stacks: dict[TechnologyStack, StackCatalogPolicy]
    products: list[EquipmentCatalogItem] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_catalog(self) -> "EquipmentCatalog":
        if self.schema_version != CATALOG_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported equipment catalog schema_version: {self.schema_version}"
            )
        if set(self.stacks) != {"i3", "knx"}:
            raise ValueError("equipment catalog must define exactly i3 and knx stacks")

        products_by_id = {item.product_id: item for item in self.products}
        if len(products_by_id) != len(self.products):
            raise ValueError("product_id values must be unique")

        for item in self.products:
            if item.source_checked_at > self.source_checked_at:
                raise ValueError("product source date cannot exceed catalog source date")
            unknown = set(item.requires_product_ids) - set(products_by_id)
            if unknown:
                raise ValueError(
                    f"{item.product_id} requires unknown products: "
                    + ", ".join(sorted(unknown))
                )
            wrong_stack = [
                product_id
                for product_id in item.requires_product_ids
                if products_by_id[product_id].technology_stack
                != item.technology_stack
            ]
            if wrong_stack:
                raise ValueError(
                    f"{item.product_id} has cross-stack prerequisites: "
                    + ", ".join(sorted(wrong_stack))
                )
            non_mandatory_prerequisites = [
                product_id
                for product_id in item.requires_product_ids
                if product_id
                not in self.stacks[item.technology_stack].mandatory_product_ids
            ]
            if non_mandatory_prerequisites:
                raise ValueError(
                    f"{item.product_id} has non-mandatory selection prerequisites: "
                    + ", ".join(sorted(non_mandatory_prerequisites))
                )

        for stack, policy in self.stacks.items():
            for product_id in policy.mandatory_product_ids:
                product = products_by_id.get(product_id)
                if product is None:
                    raise ValueError(
                        f"{stack} references unknown mandatory product: {product_id}"
                    )
                if product.technology_stack != stack:
                    raise ValueError(
                        f"{product_id} is mandatory for the wrong technology stack"
                    )
                if not product.mandatory:
                    raise ValueError(f"{product_id} must be marked mandatory")

            flagged = {
                item.product_id
                for item in self.products
                if item.technology_stack == stack and item.mandatory
            }
            if flagged != set(policy.mandatory_product_ids):
                raise ValueError(
                    f"{stack} mandatory flags must match mandatory_product_ids"
                )

            covered = {
                channel
                for item in self.products
                if item.technology_stack == stack
                for channel in item.capabilities
            }
            missing = set(ChannelType) - covered
            if missing:
                missing_names = ", ".join(sorted(item.value for item in missing))
                raise ValueError(f"{stack} does not cover channel types: {missing_names}")
        return self


def load_equipment_catalog(
    catalog_path: str | Path | None = None,
) -> EquipmentCatalog:
    project_root = Path(__file__).resolve().parents[1]
    resolved_path = Path(catalog_path) if catalog_path else (
        project_root / "config" / "equipment_catalog_v1.yaml"
    )
    try:
        raw = yaml.safe_load(resolved_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ValueError(
            f"cannot read YAML equipment catalog {resolved_path.name}: {error}"
        ) from error
    if not isinstance(raw, dict):
        raise ValueError("equipment catalog YAML must contain a mapping")
    return EquipmentCatalog.model_validate(raw)
