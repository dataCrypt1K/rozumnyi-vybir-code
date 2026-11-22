from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Literal

from pydantic import Field, model_validator

from domain.enums import ChannelType
from domain.models import DomainModel, TechnicalCalculation
from services.equipment_catalog import (
    EquipmentCatalog,
    EquipmentCatalogItem,
    load_equipment_catalog,
)


SELECTION_ALGORITHM_VERSION = "equipment_selection_v1"
CHANNEL_SELECTION_ORDER = (
    ChannelType.TRIAC,
    ChannelType.PWM,
    ChannelType.DALI_ADDRESS,
    ChannelType.DI,
    ChannelType.RO,
)


class SelectedEquipmentProduct(DomainModel):
    product_id: str = Field(min_length=1, max_length=160)
    manufacturer: str = Field(min_length=1, max_length=160)
    model: str = Field(min_length=1, max_length=160)
    quantity: int = Field(ge=1, le=10000)
    mandatory: bool
    unit_capabilities: dict[ChannelType, int] = Field(default_factory=dict)
    provided_capabilities: dict[ChannelType, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def provided_capabilities_match_quantity(self) -> "SelectedEquipmentProduct":
        expected = {
            channel: amount * self.quantity
            for channel, amount in self.unit_capabilities.items()
        }
        if self.provided_capabilities != expected:
            raise ValueError("provided capabilities must equal unit capacity * quantity")
        return self


class EquipmentChannelCoverage(DomainModel):
    channel_type: ChannelType
    required_total: int = Field(ge=0)
    provided_total: int = Field(ge=0)
    margin: int

    @model_validator(mode="after")
    def margin_matches_totals(self) -> "EquipmentChannelCoverage":
        if self.margin != self.provided_total - self.required_total:
            raise ValueError("coverage margin must equal provided minus required")
        return self


class EquipmentVariant(DomainModel):
    technology_stack: Literal["i3", "knx"]
    selected_products: list[SelectedEquipmentProduct] = Field(default_factory=list)
    coverage: list[EquipmentChannelCoverage] = Field(default_factory=list)
    full_coverage: bool
    unresolved_external_prerequisites: list[str] = Field(default_factory=list)
    blocked_reasons: list[str] = Field(default_factory=list)
    price_status: Literal["unavailable"] = "unavailable"
    catalog_review_status: Literal["draft_for_expert_review", "expert_reviewed"]
    final_engineering_required: Literal[True] = True

    @model_validator(mode="after")
    def coverage_status_matches_margins(self) -> "EquipmentVariant":
        if len({item.channel_type for item in self.coverage}) != len(self.coverage):
            raise ValueError("coverage channel types must be unique")
        calculated = all(item.margin >= 0 for item in self.coverage)
        if self.full_coverage != calculated:
            raise ValueError("full_coverage must match channel margins")
        if self.full_coverage and self.blocked_reasons:
            raise ValueError("a fully covered variant cannot have blocked reasons")
        return self


class EquipmentSelectionResult(DomainModel):
    algorithm_version: Literal["equipment_selection_v1"]
    catalog_id: Literal["equipment_catalog_v1"]
    source_technical_rules_version: str = Field(min_length=1, max_length=80)
    needs_review: bool
    blocked_reasons: list[str] = Field(default_factory=list)
    variants: list[EquipmentVariant] = Field(default_factory=list)

    @model_validator(mode="after")
    def review_state_is_consistent(self) -> "EquipmentSelectionResult":
        if self.needs_review != bool(self.blocked_reasons):
            raise ValueError("selection needs_review must match blocked_reasons")
        if self.needs_review and self.variants:
            raise ValueError("blocked selection must not produce equipment variants")
        if not self.needs_review:
            stacks = [variant.technology_stack for variant in self.variants]
            if stacks != ["i3", "knx"]:
                raise ValueError("successful selection must contain i3 and knx variants")
        return self


@dataclass
class _ChannelPlan:
    quantities: Counter[str] = field(default_factory=Counter)
    added_capabilities: Counter[ChannelType] = field(default_factory=Counter)

    def with_product(self, product: EquipmentCatalogItem) -> "_ChannelPlan":
        quantities = self.quantities.copy()
        quantities[product.product_id] += 1
        capabilities = self.added_capabilities.copy()
        for channel, amount in product.capabilities.items():
            capabilities[channel] += amount
        return _ChannelPlan(quantities, capabilities)


def _product_signature(quantities: Counter[str]) -> tuple[tuple[str, int], ...]:
    return tuple(sorted(quantities.items()))


def _plan_score(
    plan: _ChannelPlan,
    provided: Counter[ChannelType],
    required: dict[ChannelType, int],
) -> tuple[int, int, tuple[tuple[str, int], ...]]:
    projected = provided.copy()
    projected.update(plan.added_capabilities)
    total_excess = sum(
        max(0, projected[channel] - required[channel]) for channel in ChannelType
    )
    module_count = sum(plan.quantities.values())
    return total_excess, module_count, _product_signature(plan.quantities)


def _best_plan_for_channel(
    channel: ChannelType,
    deficit: int,
    candidates: list[EquipmentCatalogItem],
    provided: Counter[ChannelType],
    required: dict[ChannelType, int],
) -> _ChannelPlan | None:
    if deficit <= 0:
        return _ChannelPlan()
    if not candidates:
        return None

    candidates = sorted(candidates, key=lambda item: item.product_id)
    max_unit_capacity = max(item.capabilities[channel] for item in candidates)
    coverage_limit = deficit + max_unit_capacity - 1
    states: dict[int, _ChannelPlan] = {0: _ChannelPlan()}

    for covered in range(coverage_limit + 1):
        current = states.get(covered)
        if current is None:
            continue
        for product in candidates:
            next_covered = covered + product.capabilities[channel]
            if next_covered > coverage_limit:
                continue
            candidate_plan = current.with_product(product)
            existing = states.get(next_covered)
            if existing is None or _plan_score(
                candidate_plan, provided, required
            ) < _plan_score(existing, provided, required):
                states[next_covered] = candidate_plan

    complete = [
        plan for covered, plan in states.items() if covered >= deficit
    ]
    if not complete:
        return None
    return min(complete, key=lambda plan: _plan_score(plan, provided, required))


def _add_product_quantities(
    selected: Counter[str],
    provided: Counter[ChannelType],
    products_by_id: dict[str, EquipmentCatalogItem],
    quantities: Counter[str],
) -> None:
    for product_id, quantity in quantities.items():
        selected[product_id] += quantity
        product = products_by_id[product_id]
        for channel, amount in product.capabilities.items():
            provided[channel] += amount * quantity


def _build_variant(
    stack: Literal["i3", "knx"],
    required: dict[ChannelType, int],
    catalog: EquipmentCatalog,
) -> EquipmentVariant:
    stack_products = [
        item for item in catalog.products if item.technology_stack == stack
    ]
    products_by_id = {item.product_id: item for item in stack_products}
    selected: Counter[str] = Counter()
    provided: Counter[ChannelType] = Counter()

    if any(required.values()):
        mandatory = Counter(
            {product_id: 1 for product_id in catalog.stacks[stack].mandatory_product_ids}
        )
        _add_product_quantities(selected, provided, products_by_id, mandatory)

    blocked_reasons: list[str] = []
    for channel in CHANNEL_SELECTION_ORDER:
        deficit = max(0, required[channel] - provided[channel])
        if deficit == 0:
            continue
        candidates = [
            item
            for item in stack_products
            if not item.mandatory and item.capabilities.get(channel, 0) > 0
        ]
        plan = _best_plan_for_channel(
            channel, deficit, candidates, provided, required
        )
        if plan is None:
            blocked_reasons.append(
                f"{stack}:no_catalog_capacity:{channel.value}:{deficit}"
            )
            continue
        _add_product_quantities(
            selected, provided, products_by_id, plan.quantities
        )

    coverage = [
        EquipmentChannelCoverage(
            channel_type=channel,
            required_total=required[channel],
            provided_total=provided[channel],
            margin=provided[channel] - required[channel],
        )
        for channel in ChannelType
    ]
    full_coverage = all(item.margin >= 0 for item in coverage)
    if not full_coverage and not blocked_reasons:
        blocked_reasons.append(f"{stack}:incomplete_channel_coverage")

    selected_products = []
    for product_id in sorted(selected):
        product = products_by_id[product_id]
        quantity = selected[product_id]
        selected_products.append(
            SelectedEquipmentProduct(
                product_id=product.product_id,
                manufacturer=product.manufacturer,
                model=product.model,
                quantity=quantity,
                mandatory=product.mandatory,
                unit_capabilities=product.capabilities,
                provided_capabilities={
                    channel: amount * quantity
                    for channel, amount in product.capabilities.items()
                },
            )
        )

    unresolved = set()
    if selected:
        unresolved.update(catalog.stacks[stack].external_prerequisites)
        for product_id in selected:
            unresolved.update(products_by_id[product_id].external_prerequisites)

    return EquipmentVariant(
        technology_stack=stack,
        selected_products=selected_products,
        coverage=coverage,
        full_coverage=full_coverage,
        unresolved_external_prerequisites=sorted(unresolved),
        blocked_reasons=sorted(set(blocked_reasons)),
        catalog_review_status=catalog.review_status,
    )


def select_equipment_variants(
    calculation: TechnicalCalculation,
    catalog: EquipmentCatalog | None = None,
) -> EquipmentSelectionResult:
    resolved_catalog = catalog or load_equipment_catalog()
    if calculation.needs_review:
        return EquipmentSelectionResult(
            algorithm_version=SELECTION_ALGORITHM_VERSION,
            catalog_id=resolved_catalog.catalog_id,
            source_technical_rules_version=calculation.rules_version,
            needs_review=True,
            blocked_reasons=sorted(set(calculation.blocking_missing_data)),
            variants=[],
        )

    required = {channel: 0 for channel in ChannelType}
    for item in calculation.channel_requirements:
        required[item.channel_type] = item.required_total

    variants = [
        _build_variant("i3", required, resolved_catalog),
        _build_variant("knx", required, resolved_catalog),
    ]
    variant_blocks = [
        reason for variant in variants for reason in variant.blocked_reasons
    ]
    if variant_blocks:
        return EquipmentSelectionResult(
            algorithm_version=SELECTION_ALGORITHM_VERSION,
            catalog_id=resolved_catalog.catalog_id,
            source_technical_rules_version=calculation.rules_version,
            needs_review=True,
            blocked_reasons=sorted(set(variant_blocks)),
            variants=[],
        )

    return EquipmentSelectionResult(
        algorithm_version=SELECTION_ALGORITHM_VERSION,
        catalog_id=resolved_catalog.catalog_id,
        source_technical_rules_version=calculation.rules_version,
        needs_review=False,
        blocked_reasons=[],
        variants=variants,
    )
