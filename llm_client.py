from __future__ import annotations

from copy import deepcopy
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


ROOT = Path(__file__).resolve().parent

EXPLANATION_FACT_BOUNDARIES = {
    "q_visitors_absent": (
        "Це частота потреби прийняти відвідувача, коли власника немає вдома; "
        "це не частота від'їздів чи всіх візитів гостей."
    ),
    "q_outage_impact": (
        "Це наслідки відключення, а не частота відключень."
    ),
    "q_home_unattended": (
        "Часта відсутність робить своєчасне виявлення події ціннішим, "
        "але не збільшує ймовірність протікання або пожежі."
    ),
    "q_energy_checking": (
        "Рідкісна ручна перевірка споживання не є доказом бажання "
        "встановити моніторинг або зменшити витрати."
    ),
    "q_air_symptoms": (
        "Рідкісні симптоми не означають, що проблем з повітрям немає."
    ),
    "previous_intrusion": (
        "Відсутність попереднього проникнення не доводить низький майбутній ризик."
    ),
    "gas_co_sources": (
        "Відсутність джерел газу або CO не означає відсутність пожежного ризику."
    ),
    "vulnerable_residents": (
        "Наявність дітей або літніх мешканців змінює важливість наслідків, "
        "але сама по собі не підвищує частоту протікань чи пожеж."
    ),
    "private_plot": (
        "Наявність ділянки сама по собі не доводить, що на ній є насадження "
        "або що їм потрібен регулярний полив."
    ),
    "household_size": (
        "Кількість мешканців впливає на масштаб наслідків, але не доводить "
        "низьку чи високу ймовірність пожежі."
    ),
    "critical_loads": (
        "Наявність критичних навантажень підвищує цінність контролю "
        "і керування ними, але не означає, що резервне живлення вже є."
    ),
}

EXPLANATION_SYSTEM_BOUNDARIES = {
    "power_protection_monitoring": (
        "Електрощит показує можливість встановити обладнання, а не доводить "
        "потребу в моніторингу. Часті від'їзди можуть підвищувати користь "
        "дистанційного перегляду стану мережі, але не частоту аварій. Не обіцяй "
        "сповіщення саме під час відключення без підтвердженого резерву "
        "живлення контролера і каналу зв'язку. Моніторинг не дорівнює "
        "автоматичному електрозахисту."
    ),
}


def load_env_file(path: Path = ROOT / ".env") -> None:
    """Load simple KEY=VALUE entries without overriding process environment."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value[:1] == value[-1:] and value.startswith(("\"", "'")):
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


class LLMRecommendation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    system_id: str = Field(min_length=1)
    llm_score: int = Field(ge=0, le=100)
    reason: str = Field(min_length=1, max_length=400)
    evidence_question_ids: list[str]
    missing_data: list[str]

    @field_validator("evidence_question_ids", "missing_data")
    @classmethod
    def unique_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("question ids must be unique")
        return value


class LLMResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recommendations: list[LLMRecommendation]


class ExplanationEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_id: str = Field(min_length=1)
    answer_json: str = Field(min_length=1)


class LLMExplanation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    system_id: str = Field(min_length=1)
    final_status: str = Field(pattern=r"^(recommended|optional|inexpedient)$")
    final_score: int = Field(ge=0, le=100)
    reason: str = Field(min_length=15, max_length=400)
    evidence: list[ExplanationEvidence]


class ExplanationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    explanations: list[LLMExplanation]


class PartialExplanationError(ValueError):
    def __init__(
        self, valid_explanations: list[LLMExplanation], errors: list[str]
    ) -> None:
        self.valid_explanations = valid_explanations
        super().__init__(errors[0] if errors else "Incomplete explanation response")


class LLMClient(Protocol):
    def recommend(
        self,
        profile: dict[str, Any],
        systems: list[dict[str, Any]],
        questions: list[dict[str, Any]],
        rule_results: dict[str, dict[str, Any]],
    ) -> LLMResponse: ...


OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "recommendations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "system_id": {"type": "string", "minLength": 1},
                    "llm_score": {"type": "integer", "minimum": 0, "maximum": 100},
                    "reason": {"type": "string", "minLength": 1, "maxLength": 400},
                    "evidence_question_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "missing_data": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": [
                    "system_id",
                    "llm_score",
                    "reason",
                    "evidence_question_ids",
                    "missing_data",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["recommendations"],
    "additionalProperties": False,
}


EXPLANATION_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "explanations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "system_id": {"type": "string"},
                    "final_status": {
                        "type": "string",
                        "enum": ["recommended", "optional", "inexpedient"],
                    },
                    "final_score": {"type": "integer", "minimum": 0, "maximum": 100},
                    "reason": {"type": "string", "minLength": 15, "maxLength": 400},
                    "evidence": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "question_id": {"type": "string"},
                                "answer_json": {"type": "string"},
                            },
                            "required": ["question_id", "answer_json"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": [
                    "system_id", "final_status", "final_score", "reason", "evidence"
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["explanations"],
    "additionalProperties": False,
}


def explanation_fact_ids(profile: dict[str, Any], decision: dict[str, Any]) -> list[str]:
    candidates = decision.get("explanation_candidate_fact_ids")
    if candidates is None:
        candidates = decision.get("evidence_question_ids", [])
    ids = [
        item for item in candidates
        if item in profile
    ]
    if "property_type" in profile:
        ids.insert(0, "property_type")
    return list(dict.fromkeys(ids))


def _obvious_status_conflict(status: str, reason: str) -> bool:
    first_sentence = reason.casefold().split(".", 1)[0]
    if status == "recommended":
        return bool(re.search(r"\b(?:не рекоменду|не варто|недоцільн)", first_sentence))
    if status == "inexpedient":
        positive = re.search(
            r"\b(?:рекомендую|рекомендуємо|доцільно|варто|важлив\w*)",
            first_sentence,
        )
        if positive and not re.search(
            r"\bне(?:\s+є)?\s*$", first_sentence[max(0, positive.start() - 12):positive.start()]
        ):
            return True
    if status == "optional":
        return bool(re.search(r"\b(?:недоцільн\w*|не\s+є\s+доцільн\w*)", first_sentence))
    return False


def _obvious_fact_conflict(
    reason: str,
    evidence_ids: list[str],
    profile: dict[str, Any],
    system_id: str = "",
) -> str | None:
    text = reason.casefold()
    if re.search(
        r"(?:немає|не\s+має|відсутні)\s+(?:\w+\s+){0,2}ризик\w*",
        text,
    ):
        return "negative answers do not establish absence of safety risk"
    if (
        system_id == "leak_protection"
        and "q_home_unattended" in evidence_ids
        and re.search(
            r"(?:відсутн\w*|без\s+нагляд\w*).{0,100}"
            r"(?:підвищ\w*|збільш\w*).{0,35}ризик\w*.{0,45}"
            r"(?:протікан\w*|водн\w*)",
            text,
        )
    ):
        return "absence raises detection value, not leak probability"
    if (
        system_id == "fire_safety"
        and "vulnerable_residents" in evidence_ids
        and re.search(
            r"(?:діт\w*|літн\w*).{0,70}"
            r"(?:підвищ\w*|збільш\w*).{0,40}ризик\w*",
            text,
        )
    ):
        return "vulnerable residents affect consequences, not fire probability"
    if re.search(r"низьк\w*\s+ймовірн\w*\s+(?:проникнен\w*|інцидент\w*)", text):
        return "past incidents do not establish future incident probability"
    if re.search(r"відсутн\w*.*(?:проникнен\w*|джерел\w*\s+газу).*підвищ\w*", text):
        return "absence of an incident or gas source cannot increase risk"
    if re.search(r"ризик\w*.*відсутн\w*\s+джерел\w*\s+газу", text):
        return "absence of a gas source is not a fire-risk factor"
    if re.search(r"низьк\w*\s+ризик\w*\s+пожеж", text):
        return "these answers do not establish a low fire risk"
    if (
        system_id == "fire_safety"
        and re.search(
            r"(?:немає|не\s+було|відсутн\w*|попередніх|попередні|"
            r"раніше|в\s+минулому).{0,55}(?:інцидент\w*|пожеж\w*)"
            r"|(?:інцидент\w*|пожеж\w*).{0,55}(?:не\s+було|немає)",
            text,
        )
    ):
        return "the profile has no history of fire incidents"
    if (
        "q_air_symptoms" in evidence_ids
        and profile.get("q_air_symptoms") in {"rarely", "sometimes"}
        and re.search(r"(?:немає|відсутн\w*)\s+проблем\w*\s+з\s+якістю\s+повітря", text)
    ):
        return "rare air-quality symptoms do not mean no symptoms"
    if (
        "q_outage_impact" in evidence_ids
        and "outage_frequency" not in evidence_ids
        and re.search(
            r"(?:частот\w*|част\w*|рідк\w*|щомісяц\w*|щотижн\w*)"
            r".{0,45}відключенн\w*|відключенн\w*.{0,45}"
            r"(?:частот\w*|част\w*|рідк\w*|щомісяц\w*|щотижн\w*)",
            text,
        )
    ):
        return "outage impact does not establish outage frequency"
    if (
        "q_visitors_absent" in evidence_ids
        and "q_home_unattended" not in evidence_ids
        and re.search(r"(?:част\w*\s+відсутност\w*|рідк\w*\s+візит\w*\s+гост\w*)", text)
    ):
        return "visitors while absent do not establish travel or visit frequency"
    return None


def validate_explanation_response(
    payload: dict[str, Any],
    decisions: list[dict[str, Any]],
    profile: dict[str, Any],
) -> ExplanationResponse:
    try:
        response = ExplanationResponse.model_validate(payload)
    except ValidationError as error:
        raise ValueError(f"Invalid LLM explanation response: {error}") from error
    expected = {item["system_id"]: item for item in decisions}
    actual_ids = [item.system_id for item in response.explanations]
    if len(actual_ids) != len(set(actual_ids)) or set(actual_ids) != set(expected):
        raise ValueError("LLM explanation system ids do not match final decisions")
    for item in response.explanations:
        decision = expected[item.system_id]
        if (
            item.final_status != decision["status"]
            or item.final_score != decision["suitability_percent"]
        ):
            raise ValueError(f"{item.system_id}: explanation contradicts final decision")
        allowed = set(explanation_fact_ids(profile, decision))
        evidence_ids = [fact.question_id for fact in item.evidence]
        if allowed and not evidence_ids:
            raise ValueError(f"{item.system_id}: explanation has no supporting facts")
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError(f"{item.system_id}: duplicate explanation evidence")
        for fact in item.evidence:
            if fact.question_id not in allowed:
                raise ValueError(f"{item.system_id}: explanation cites an unsupported fact")
            try:
                asserted = json.loads(fact.answer_json)
            except json.JSONDecodeError as error:
                raise ValueError(f"{item.system_id}: invalid evidence answer_json") from error
            if asserted != profile[fact.question_id]:
                raise ValueError(f"{item.system_id}: explanation cites a false answer")
        specific_facts = allowed - {"property_type"}
        if specific_facts and not specific_facts.intersection(evidence_ids):
            raise ValueError(
                f"{item.system_id}: explanation has no system-specific supporting fact"
            )
        if item.system_id == "power_protection_monitoring" and item.final_status == "recommended":
            need_facts = {
                "q_home_unattended", "q_energy_checking", "q_outage_impact",
                "outage_frequency", "critical_loads",
            }
            if not need_facts.intersection(evidence_ids):
                raise ValueError(
                    "power_protection_monitoring: an electrical panel alone "
                    "does not establish user need"
                )
        if _obvious_status_conflict(item.final_status, item.reason):
            raise ValueError(f"{item.system_id}: explanation wording contradicts status")
        fact_conflict = _obvious_fact_conflict(
            item.reason, evidence_ids, profile, item.system_id
        )
        if fact_conflict:
            raise ValueError(f"{item.system_id}: {fact_conflict}")
    return response


def validate_response(
    payload: dict[str, Any],
    expected_system_ids: set[str],
    profile_question_ids: set[str],
) -> LLMResponse:
    try:
        response = LLMResponse.model_validate(payload)
    except ValidationError as error:
        raise ValueError(f"Invalid LLM response: {error}") from error

    actual_ids = [item.system_id for item in response.recommendations]
    if len(actual_ids) != len(set(actual_ids)):
        raise ValueError("LLM response contains duplicate system_id values")
    if set(actual_ids) != expected_system_ids:
        raise ValueError(
            "LLM system ids do not match request: "
            f"expected={sorted(expected_system_ids)}, actual={sorted(actual_ids)}"
        )

    for item in response.recommendations:
        invalid_evidence = set(item.evidence_question_ids) - profile_question_ids
        if invalid_evidence:
            raise ValueError(
                f"{item.system_id}: evidence references unknown questions "
                f"{sorted(invalid_evidence)}"
            )
    return response


def extract_output_text(response_payload: dict[str, Any]) -> str:
    texts = []
    refusals = []
    for item in response_payload.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") == "output_text":
                texts.append(content.get("text", ""))
            if content.get("type") == "refusal":
                refusals.append(content.get("refusal", "Model refused the request"))
    if refusals:
        raise RuntimeError("; ".join(refusals))
    if not texts:
        raise RuntimeError("OpenAI response does not contain output_text")
    return "".join(texts)


class OpenAIResponsesClient:
    def __init__(self, config: dict[str, Any]) -> None:
        load_env_file()
        self.endpoint = config["endpoint"]
        self.model = os.getenv("OPENAI_MODEL", config["model"])
        self.timeout_seconds = config.get("timeout_seconds", 60)
        self.max_output_tokens = int(config.get("max_output_tokens", 6000))
        self.temperature = float(config.get("temperature", 0))
        self.store = bool(config.get("store", False))
        self.api_key = os.getenv("OPENAI_API_KEY")
        if not self.api_key or self.api_key == "PASTE_YOUR_KEY_HERE":
            raise RuntimeError(
                "OPENAI_API_KEY is not configured. Put the key in prototype/.env "
                "or use provider=mock for local integration tests."
            )
        self.instructions = (ROOT / "prompts" / "system_prompt_uk.txt").read_text(
            encoding="utf-8"
        )
        self.explanation_instructions = (
            ROOT / "prompts" / "explanation_prompt_uk.txt"
        ).read_text(encoding="utf-8")

    def build_request(
        self,
        profile: dict[str, Any],
        systems: list[dict[str, Any]],
        questions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        allowed_system_ids = [item["system_id"] for item in systems]
        response_schema = deepcopy(OUTPUT_SCHEMA)
        recommendations_schema = response_schema["properties"]["recommendations"]
        recommendations_schema["minItems"] = len(allowed_system_ids)
        recommendations_schema["maxItems"] = len(allowed_system_ids)
        recommendations_schema["items"]["properties"]["system_id"] = {
            "type": "string",
            "enum": allowed_system_ids,
        }
        input_payload = {
            "profile": [
                {
                    "question_id": question["id"],
                    "question": question["text"],
                    "type": question["type"],
                    "answer": profile[question["id"]],
                    **(
                        {"min": question["min"], "max": question["max"]}
                        if "min" in question and "max" in question
                        else {}
                    ),
                    **(
                        {"options": question["options"]}
                        if "options" in question
                        else {}
                    ),
                    **(
                        {"option_scores": question["option_scores"]}
                        if "option_scores" in question
                        else {}
                    ),
                }
                for question in questions
                if question["id"] in profile
            ],
            "systems": [
                {
                    "system_id": item["system_id"],
                    "name_uk": item["name_uk"],
                    "description": item["description"],
                    "included_features": item.get("included_features", []),
                }
                for item in systems
            ],
        }
        return {
            "model": self.model,
            "instructions": self.instructions,
            "input": json.dumps(input_payload, ensure_ascii=False),
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "smart_home_recommendations",
                    "strict": True,
                    "schema": response_schema,
                }
            },
            "max_output_tokens": getattr(self, "max_output_tokens", 6000),
            "temperature": getattr(self, "temperature", 0),
            "store": self.store,
        }

    def recommend(
        self,
        profile: dict[str, Any],
        systems: list[dict[str, Any]],
        questions: list[dict[str, Any]],
        rule_results: dict[str, dict[str, Any]],
    ) -> LLMResponse:
        del rule_results  # The live LLM score is independent from the rule score.
        request_payload = self.build_request(profile, systems, questions)
        parsed_payload = self._post_json(request_payload)
        expected_ids = {item["system_id"] for item in systems}
        return validate_response(parsed_payload, expected_ids, set(profile))

    def build_explanation_request(
        self,
        profile: dict[str, Any],
        systems: list[dict[str, Any]],
        questions: list[dict[str, Any]],
        decisions: list[dict[str, Any]],
        feedback: str | None = None,
    ) -> dict[str, Any]:
        systems_by_id = {item["system_id"]: item for item in systems}
        questions_by_id = {item["id"]: item for item in questions}
        schema = deepcopy(EXPLANATION_OUTPUT_SCHEMA)
        explanations = schema["properties"]["explanations"]
        explanations["minItems"] = len(decisions)
        explanations["maxItems"] = len(decisions)
        explanations["items"]["properties"]["system_id"] = {
            "type": "string",
            "enum": [item["system_id"] for item in decisions],
        }
        input_payload = {
            "decisions": [
                {
                    "system_id": item["system_id"],
                    "system_name": systems_by_id[item["system_id"]]["name_uk"],
                    "final_status": item["status"],
                    "final_score": item["suitability_percent"],
                    **(
                        {"system_interpretation_boundary":
                         EXPLANATION_SYSTEM_BOUNDARIES[item["system_id"]]}
                        if item["system_id"] in EXPLANATION_SYSTEM_BOUNDARIES
                        else {}
                    ),
                    "available_facts": [
                        {
                            "question_id": question_id,
                            "question": questions_by_id.get(question_id, {}).get(
                                "text", question_id
                            ),
                            "answer_json": json.dumps(
                                profile[question_id], ensure_ascii=False, sort_keys=True
                            ),
                            **(
                                {"interpretation_boundary": EXPLANATION_FACT_BOUNDARIES[question_id]}
                                if question_id in EXPLANATION_FACT_BOUNDARIES
                                else {}
                            ),
                        }
                        for question_id in explanation_fact_ids(profile, item)
                    ],
                }
                for item in decisions
            ],
            "correction_feedback": feedback,
        }
        return {
            "model": self.model,
            "instructions": self.explanation_instructions,
            "input": json.dumps(input_payload, ensure_ascii=False),
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "smart_home_final_explanations",
                    "strict": True,
                    "schema": schema,
                }
            },
            "max_output_tokens": self.max_output_tokens,
            "temperature": self.temperature,
            "store": self.store,
        }

    def explain(
        self,
        profile: dict[str, Any],
        systems: list[dict[str, Any]],
        questions: list[dict[str, Any]],
        decisions: list[dict[str, Any]],
        feedback: str | None = None,
    ) -> ExplanationResponse:
        request_payload = self.build_explanation_request(
            profile, systems, questions, decisions, feedback
        )
        parsed_payload = self._post_json(request_payload)
        try:
            return validate_explanation_response(parsed_payload, decisions, profile)
        except ValueError as error:
            try:
                parsed = ExplanationResponse.model_validate(parsed_payload)
            except ValidationError:
                raise error
            expected = {item["system_id"]: item for item in decisions}
            counts = {
                system_id: sum(
                    item.system_id == system_id for item in parsed.explanations
                )
                for system_id in expected
            }
            valid = []
            errors = []
            for item in parsed.explanations:
                if item.system_id not in expected or counts[item.system_id] != 1:
                    continue
                try:
                    validate_explanation_response(
                        {"explanations": [item.model_dump(mode="json")]},
                        [expected[item.system_id]],
                        profile,
                    )
                except ValueError as item_error:
                    errors.append(str(item_error))
                else:
                    valid.append(item)
            if valid:
                raise PartialExplanationError(valid, errors) from error
            raise error

    def _post_json(self, request_payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(request_payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            details = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"OpenAI API error {error.code}: {details}") from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"OpenAI API connection error: {error.reason}") from error

        output_text = extract_output_text(response_payload)
        try:
            parsed_payload = json.loads(output_text)
        except json.JSONDecodeError as error:
            raise ValueError("OpenAI output_text is not valid JSON") from error
        return parsed_payload


class MockLLMClient:
    """Deterministic integration stub. It is not an independent model evaluation."""

    OFFSETS = {
        "climate_control": -6,
        "smart_lighting": 4,
        "leak_protection": -3,
    }

    def recommend(
        self,
        profile: dict[str, Any],
        systems: list[dict[str, Any]],
        questions: list[dict[str, Any]],
        rule_results: dict[str, dict[str, Any]],
    ) -> LLMResponse:
        del questions
        recommendations = []
        for system in systems:
            system_id = system["system_id"]
            rule_result = rule_results[system_id]
            score = max(
                0,
                min(100, int(rule_result["rule_score"]) + self.OFFSETS.get(system_id, 0)),
            )
            evidence = rule_result.get("evidence_question_ids", [])[:3]
            if not evidence:
                for rule_id in rule_result.get("triggered_rules", [])[:3]:
                    if rule_id.startswith("CLIMATE"):
                        evidence.append("comfort_priority")
                    elif rule_id.startswith("LIGHT"):
                        evidence.append("automation_interest")
                    elif rule_id.startswith("LEAK"):
                        evidence.append("safety_priority")
            evidence = list(dict.fromkeys(item for item in evidence if item in profile))
            reasons = rule_result.get("reasons", [])
            reason = reasons[0] if reasons else "Профіль не містить сильних факторів."
            recommendations.append(
                {
                    "system_id": system_id,
                    "llm_score": score,
                    "reason": reason,
                    "evidence_question_ids": evidence,
                    "missing_data": [],
                }
            )
        payload = {"recommendations": recommendations}
        return validate_response(
            payload,
            {item["system_id"] for item in systems},
            set(profile),
        )
