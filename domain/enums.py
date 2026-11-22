from __future__ import annotations

from enum import Enum


class StringEnum(str, Enum):
    """Python 3.10-compatible string enum."""


class PropertyType(StringEnum):
    APARTMENT = "apartment"
    HOUSE = "house"


class WorkflowStage(StringEnum):
    DRAFT = "draft"
    RECOMMENDATION_COMPLETE = "recommendation_complete"
    OBJECT_COMPLETE = "object_complete"
    FUNCTIONS_COMPLETE = "functions_complete"
    BUDGET_COMPLETE = "budget_complete"
    SCOPE_COMPLETE = "scope_complete"
    FACTS_REVIEW = "facts_review"
    TECHNICAL_INPUT = "technical_input"
    SPECIFICATION_COMPLETE = "specification_complete"


class RoomType(StringEnum):
    LIVING_ROOM = "living_room"
    KITCHEN = "kitchen"
    KITCHEN_LIVING_ROOM = "kitchen_living_room"
    BEDROOM = "bedroom"
    GUEST_ROOM = "guest_room"
    CHILDRENS_ROOM = "childrens_room"
    DINING_ROOM = "dining_room"
    BATHROOM = "bathroom"
    WC = "wc"
    HALLWAY = "hallway"
    OFFICE = "office"
    WARDROBE = "wardrobe"
    UTILITY_ROOM = "utility_room"
    LAUNDRY = "laundry"
    PANTRY = "pantry"
    STORAGE_ROOM = "storage_room"
    BOILER_ROOM = "boiler_room"
    TECHNICAL_ROOM = "technical_room"
    GARAGE = "garage"
    TERRACE = "terrace"
    BALCONY = "balcony"
    LOGGIA = "loggia"
    STAIRCASE = "staircase"
    ATTIC = "attic"
    BASEMENT = "basement"
    WORKSHOP = "workshop"
    GYM = "gym"
    CINEMA_ROOM = "cinema_room"
    SAUNA = "sauna"
    POOL_ROOM = "pool_room"
    WHOLE_PROPERTY = "whole_property"
    ENTRANCE = "entrance"
    ELECTRICAL_PANEL = "electrical_panel"
    WATER_ENTRY = "water_entry"
    PLOT = "plot"
    PERIMETER = "perimeter"
    DRIVEWAY = "driveway"
    GATE = "gate"
    WICKET = "wicket"
    GARDEN = "garden"
    LAWN = "lawn"
    OUTDOOR_PARKING = "outdoor_parking"
    ROOF = "roof"
    GUTTER = "gutter"
    WELL = "well"
    SEPTIC = "septic"
    OTHER = "other"


class LocationKind(StringEnum):
    ROOM = "room"
    SPECIAL = "special"


class EvidenceSource(StringEnum):
    MANUAL = "manual"
    QUESTIONNAIRE = "questionnaire"
    DOCUMENT_EXPLICIT = "document_explicit"
    DOCUMENT_INFERRED = "document_inferred"


class EvidenceType(StringEnum):
    USER_PROVIDED = "user_provided"
    QUESTIONNAIRE_ANSWER = "questionnaire_answer"
    EXPLICIT_TEXT = "explicit_text"
    EXPLICIT_SYMBOL = "explicit_symbol"
    VISUAL_INFERENCE = "visual_inference"


class ReliabilityLevel(StringEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ReviewStatus(StringEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    CORRECTED = "corrected"
    REJECTED = "rejected"


class RecommendationStatus(StringEnum):
    RECOMMENDED = "recommended"
    OPTIONAL = "optional"
    INEXPEDIENT = "inexpedient"
    NOT_APPLICABLE = "not_applicable"


class ImplementationPhase(StringEnum):
    FIRST = "first"
    SECOND = "second"
    LATER = "later"


class ProjectStep(StringEnum):
    OBJECT = "object"
    FUNCTIONS = "functions"
    BUDGET = "budget"


class DecisionSource(StringEnum):
    SYSTEM_DEFAULT = "system_default"
    USER = "user"
    LEGACY_IMPORT = "legacy_import"


class BudgetLevel(StringEnum):
    BASIC = "basic"
    BALANCED = "balanced"
    FLEXIBLE = "flexible"


class TechnicalInputSource(StringEnum):
    PLAN = "plan"
    QUESTIONNAIRE = "questionnaire"
    USER_CORRECTION = "user_correction"


class ValveProfile(StringEnum):
    SINGLE_OUTPUT = "single_output"
    REVERSIBLE_TWO_OUTPUT = "reversible_two_output"


class ChannelType(StringEnum):
    RO = "RO"
    DI = "DI"
    TRIAC = "TRIAC"
    PWM = "PWM"
    DALI_ADDRESS = "DALI_ADDRESS"


class PhysicalDeviceType(StringEnum):
    FLOOR_TEMPERATURE_SENSOR = "floor_temperature_sensor"
    LEAK_SENSOR = "leak_sensor"
    VALVE = "valve"
    EXHAUST_FAN = "exhaust_fan"
