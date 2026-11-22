from __future__ import annotations

from services.function_catalog import FunctionCatalog, FunctionCatalogItem


LEGACY_FUNCTIONS = {
    "climate.electric_underfloor_heating": FunctionCatalogItem(
        function_id="climate.electric_underfloor_heating",
        system_id="climate_control",
        name_uk="Керування електричною теплою підлогою",
        description_uk="Незалежне підтримання температури електричної теплої підлоги.",
        applicable_property_types=["apartment", "house"],
        applicable_room_types=["bathroom", "toilet", "kitchen", "hallway", "living_room", "bedroom", "other"],
        applicable_location_types=[],
        selection_unit="thermal_zone",
        default_phase_priority="first",
        prerequisite_function_ids=[],
        technical_mvp=True,
        evidence_requirement="both",
        final_engineering_required=True,
    ),
    "climate.hydronic_underfloor_zone_control": FunctionCatalogItem(
        function_id="climate.hydronic_underfloor_zone_control",
        system_id="climate_control",
        name_uk="Зональне керування водяною теплою підлогою",
        description_uk="Керування тепловою зоною через колектор і виконавчі пристрої.",
        applicable_property_types=["apartment", "house"],
        applicable_room_types=["bathroom", "kitchen", "hallway", "living_room", "bedroom", "childrens_room", "home_office", "other"],
        applicable_location_types=["manifold_location"],
        selection_unit="thermal_zone",
        default_phase_priority="first",
        prerequisite_function_ids=[],
        technical_mvp=False,
        evidence_requirement="both",
        final_engineering_required=True,
    ),
}


def catalog_for_saved_functions(
    catalog: FunctionCatalog, function_ids: set[str]
) -> FunctionCatalog:
    known = {item.function_id for item in catalog.functions}
    missing = function_ids - known
    unsupported = missing - LEGACY_FUNCTIONS.keys()
    if unsupported:
        raise ValueError("Збережений проєкт містить невідомі функції: " + ", ".join(sorted(unsupported)))
    if not missing:
        return catalog
    return catalog.model_copy(
        update={
            "functions": catalog.functions
            + [LEGACY_FUNCTIONS[function_id] for function_id in sorted(missing)]
        }
    )
