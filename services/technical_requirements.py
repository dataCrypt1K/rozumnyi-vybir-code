from __future__ import annotations

from decimal import Decimal, ROUND_CEILING
from typing import Protocol

from domain.enums import ChannelType, PhysicalDeviceType, ValveProfile
from domain.models import (
    Assessment,
    ChannelContribution,
    ChannelRequirement,
    PhysicalDeviceRequirement,
    TechnicalCalculation,
    TechnicalLocationInput,
)


TECHNICAL_RULES_VERSION = "technical_channels_v2"
RESERVE_RATIO = Decimal("0.20")
TECHNICAL_FUNCTION_IDS = frozenset(
    {
        "climate.electric_underfloor_heating",
        "ventilation.exhaust_fan_control",
        "lighting.switched_group_control",
        "lighting.dimmable_group_control",
        "leak.detection_and_shutoff",
    }
)
RESERVED_CHANNEL_TYPES = (
    ChannelType.RO,
    ChannelType.DI,
    ChannelType.TRIAC,
    ChannelType.PWM,
)
ALL_CHANNEL_TYPES = (*RESERVED_CHANNEL_TYPES, ChannelType.DALI_ADDRESS)


REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "climate.electric_underfloor_heating": (
        "floor_zone_count",
        "floor_temperature_sensor_count",
    ),
    "ventilation.exhaust_fan_control": ("fan_count", "fan_group_count"),
    "lighting.switched_group_control": ("relay_group_count",),
    "lighting.dimmable_group_control": (
        "triac_group_count",
        "pwm_group_count",
        "dali_group_count",
        "dali_address_count",
    ),
    "leak.detection_and_shutoff": ("leak_sensor_count",),
}


class TechnicalStepRepository(Protocol):
    def save_technical_step(
        self,
        assessment_id: str,
        expected_revision: int,
        *,
        calculation: TechnicalCalculation,
    ) -> Assessment: ...


def _selected_technical_keys(assessment: Assessment) -> set[tuple[str, str]]:
    return {
        (choice.function_id, choice.room_id)
        for choice in assessment.function_choices
        if choice.selected and choice.function_id in TECHNICAL_FUNCTION_IDS
    }


def _missing_key(item: TechnicalLocationInput, field_name: str) -> str:
    return f"{item.function_id}:{item.room_id}:{field_name}"


def _add_contribution(
    contributions: list[ChannelContribution],
    item: TechnicalLocationInput,
    field_name: str,
    channel_type: ChannelType,
    base_units: int,
) -> None:
    value = getattr(item, field_name)
    if value is None:
        return
    contributions.append(
        ChannelContribution(
            function_id=item.function_id,
            room_id=item.room_id,
            input_field=field_name,
            input_value=value,
            channel_type=channel_type,
            base_units=base_units,
        )
    )


def build_technical_calculation(
    assessment: Assessment,
    inputs: list[TechnicalLocationInput],
) -> TechnicalCalculation:
    selected_keys = _selected_technical_keys(assessment)
    input_map = {(item.function_id, item.room_id): item for item in inputs}
    if len(input_map) != len(inputs):
        raise ValueError("technical input function_id and room_id pairs must be unique")
    extra_keys = set(input_map) - selected_keys
    if extra_keys:
        raise ValueError(
            "technical inputs reference functions that are not selected: "
            + ", ".join(f"{function_id}@{room_id}" for function_id, room_id in sorted(extra_keys))
        )

    room_ids = {room.room_id for room in assessment.rooms}
    if any(room_id not in room_ids for _, room_id in input_map):
        raise ValueError("technical inputs reference an unknown room")

    missing: list[str] = []
    contributions: list[ChannelContribution] = []
    physical_counts = {device_type: 0 for device_type in PhysicalDeviceType}

    for function_id, room_id in sorted(selected_keys):
        item = input_map.get((function_id, room_id))
        required_fields = REQUIRED_FIELDS[function_id]
        if item is None:
            missing.extend(f"{function_id}:{room_id}:{field}" for field in required_fields)
            continue
        if not item.confirmed_by_user:
            missing.append(_missing_key(item, "confirmed_by_user"))
            continue
        for field_name in required_fields:
            if getattr(item, field_name) is None:
                missing.append(_missing_key(item, field_name))

        if function_id == "climate.electric_underfloor_heating":
            if item.floor_zone_count is not None:
                _add_contribution(
                    contributions, item, "floor_zone_count", ChannelType.RO, item.floor_zone_count
                )
            if item.floor_temperature_sensor_count is not None:
                _add_contribution(
                    contributions,
                    item,
                    "floor_temperature_sensor_count",
                    ChannelType.DI,
                    item.floor_temperature_sensor_count,
                )
                physical_counts[PhysicalDeviceType.FLOOR_TEMPERATURE_SENSOR] += (
                    item.floor_temperature_sensor_count
                )
        elif function_id == "lighting.switched_group_control":
            if item.relay_group_count is not None:
                _add_contribution(
                    contributions,
                    item,
                    "relay_group_count",
                    ChannelType.RO,
                    item.relay_group_count,
                )
        elif function_id == "lighting.dimmable_group_control":
            if item.triac_group_count is not None:
                _add_contribution(
                    contributions,
                    item,
                    "triac_group_count",
                    ChannelType.TRIAC,
                    item.triac_group_count,
                )
            if item.pwm_group_count is not None:
                _add_contribution(
                    contributions,
                    item,
                    "pwm_group_count",
                    ChannelType.PWM,
                    item.pwm_group_count,
                )
            if item.dali_address_count is not None:
                _add_contribution(
                    contributions,
                    item,
                    "dali_address_count",
                    ChannelType.DALI_ADDRESS,
                    item.dali_address_count,
                )
        elif function_id == "leak.detection_and_shutoff":
            if item.leak_sensor_count is not None:
                _add_contribution(
                    contributions,
                    item,
                    "leak_sensor_count",
                    ChannelType.DI,
                    item.leak_sensor_count,
                )
                physical_counts[PhysicalDeviceType.LEAK_SENSOR] += item.leak_sensor_count
        elif function_id == "ventilation.exhaust_fan_control":
            if item.fan_count is not None:
                physical_counts[PhysicalDeviceType.EXHAUST_FAN] += item.fan_count
            if item.fan_group_count is not None:
                _add_contribution(
                    contributions,
                    item,
                    "fan_group_count",
                    ChannelType.RO,
                    item.fan_group_count,
                )

    leak_keys = sorted(
        key for key in selected_keys if key[0] == "leak.detection_and_shutoff"
    )
    if leak_keys and assessment.answers.get("water_entry_present") is True:
        confirmed_leak_inputs = [
            input_map[key]
            for key in leak_keys
            if key in input_map and input_map[key].confirmed_by_user
        ]
        valve_input = next(
            (
                item
                for item in confirmed_leak_inputs
                if item.valve_count is not None or item.valve_profile is not None
            ),
            None,
        )
        if valve_input is None or valve_input.valve_count is None:
            missing.append("leak.detection_and_shutoff:water_entry:valve_count")
        if valve_input is None or valve_input.valve_profile is None:
            missing.append("leak.detection_and_shutoff:water_entry:valve_profile")
        if valve_input is not None and valve_input.valve_count is not None:
            physical_counts[PhysicalDeviceType.VALVE] += valve_input.valve_count
            if valve_input.valve_profile is not None:
                multiplier = (
                    1
                    if valve_input.valve_profile == ValveProfile.SINGLE_OUTPUT
                    else 2
                )
                _add_contribution(
                    contributions,
                    valve_input,
                    "valve_count",
                    ChannelType.RO,
                    valve_input.valve_count * multiplier,
                )

    bases = {channel_type: 0 for channel_type in ALL_CHANNEL_TYPES}
    for contribution in contributions:
        bases[contribution.channel_type] += contribution.base_units

    channel_requirements: list[ChannelRequirement] = []
    for channel_type in ALL_CHANNEL_TYPES:
        base = bases[channel_type]
        required_total = (
            int((Decimal(base) * (Decimal("1") + RESERVE_RATIO)).to_integral_value(rounding=ROUND_CEILING))
            if channel_type in RESERVED_CHANNEL_TYPES
            else base
        )
        channel_requirements.append(
            ChannelRequirement(
                channel_type=channel_type,
                base=base,
                reserve=required_total - base,
                required_total=required_total,
            )
        )

    return TechnicalCalculation(
        rules_version=TECHNICAL_RULES_VERSION,
        reserve_ratio=RESERVE_RATIO,
        inputs=inputs,
        contributions=contributions,
        channel_requirements=channel_requirements,
        physical_devices=[
            PhysicalDeviceRequirement(device_type=device_type, count=count)
            for device_type, count in physical_counts.items()
        ],
        needs_review=bool(missing),
        blocking_missing_data=missing,
    )


def persist_technical_step_if_submitted(
    repository: TechnicalStepRepository,
    assessment: Assessment,
    inputs: list[TechnicalLocationInput],
    *,
    submitted: bool,
) -> Assessment | None:
    if not submitted:
        return None
    calculation = build_technical_calculation(assessment, inputs)
    return repository.save_technical_step(
        assessment.assessment_id,
        assessment.revision,
        calculation=calculation,
    )
