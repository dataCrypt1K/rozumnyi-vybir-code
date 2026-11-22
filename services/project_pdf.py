from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from importlib.util import find_spec
from io import BytesIO
import json
from pathlib import Path
import re
import unicodedata
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    CondPageBreak,
    LongTable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from domain.enums import BudgetLevel, ImplementationPhase, PropertyType, RecommendationStatus
from domain.models import Assessment
from services.budget_summary import (
    ProjectSummary,
    build_project_summary,
    empty_scope_guidance,
    group_scenario_functions,
)
from services.function_catalog import FunctionCatalog


STATUS_LABELS = {
    RecommendationStatus.RECOMMENDED: "Рекомендована",
    RecommendationStatus.OPTIONAL: "Опційна",
    RecommendationStatus.INEXPEDIENT: "Недоцільна",
    RecommendationStatus.NOT_APPLICABLE: "Не застосовна",
}

PHASE_LABELS = {
    ImplementationPhase.FIRST: "У першу чергу",
    ImplementationPhase.SECOND: "У другу чергу",
    ImplementationPhase.LATER: "При бажанні",
}

BUDGET_LABELS = {
    BudgetLevel.BASIC: "Обмежений бюджет",
    BudgetLevel.BALANCED: "Збалансований бюджет",
    BudgetLevel.FLEXIBLE: "Гнучкий бюджет",
}

BUDGET_DESCRIPTIONS = {
    BudgetLevel.BASIC: "Поточний фокус - вибрані функції першої черги.",
    BudgetLevel.BALANCED: "Поточний фокус - вибрані функції першої та другої черги.",
    BudgetLevel.FLEXIBLE: "У поточному фокусі весь вибраний обсяг у збережених фазах.",
}

FOCUS_LABELS = {
    "current_focus": "Поточний фокус",
    "next_stage": "Наступний етап",
    "later_or_optional": "Пізніше або за бажанням",
}

STATIC_TEXT = """
Попередній проєкт розумного дому
Метадані проєкту Назва Ідентифікатор Тип житла Площа Рівні Ревізія Оновлено
Версії конфігурацій Оцінка систем Система Статус Доцільність Пояснення
Вибрані функції Функція Локація Рекомендований статус Рекомендована фаза
Вибрана фаза Сценарний фокус Примітки Потребує перевірки Передумова
Передбачити підготовку Не вибрано
Перевірка даних і залежностей Невиконані залежності відсутні
Бюджетна позиція Заявлений користувачем орієнтир Суму не вказано
Інженерні застереження
Це попередній планувальний документ, а не проєкт, специфікація, комерційна
пропозиція або кошторис. Бюджет не змінював рекомендації чи фази і не гарантує,
що вибраний обсяг вкладається у вказану суму. Обладнання, роботи, кабелі,
сумісність, навантаження і розміщення визначає інтегратор після обстеження
об'єкта. Сторінка Квартира Будинок
"""

FONT_REGULAR = "SmartHomePdfRegular"
FONT_BOLD = "SmartHomePdfBold"


class PdfGenerationError(RuntimeError):
    pass


def _normalize_text(value: object) -> str:
    text = "" if value is None else str(value)
    for dash in ("\u2010", "\u2011", "\u2012", "\u2013", "\u2014", "\u2015"):
        text = text.replace(dash, "-")
    text = text.replace("\u00a0", " ")
    return " ".join(text.split())


def _font_candidates() -> list[tuple[Path, Path]]:
    project_root = Path(__file__).resolve().parents[1]
    candidates = [
        (
            project_root / "assets" / "fonts" / "DejaVuSans.ttf",
            project_root / "assets" / "fonts" / "DejaVuSans-Bold.ttf",
        ),
        (
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ),
        (Path("C:/Windows/Fonts/arial.ttf"), Path("C:/Windows/Fonts/arialbd.ttf")),
        (Path("C:/Windows/Fonts/calibri.ttf"), Path("C:/Windows/Fonts/calibrib.ttf")),
    ]
    try:
        matplotlib_spec = find_spec("matplotlib")
        matplotlib_roots = (
            list(matplotlib_spec.submodule_search_locations or [])
            if matplotlib_spec is not None
            else []
        )
        if not matplotlib_roots:
            return candidates
        matplotlib_fonts = Path(matplotlib_roots[0]) / "mpl-data" / "fonts" / "ttf"
        candidates.append(
            (
                matplotlib_fonts / "DejaVuSans.ttf",
                matplotlib_fonts / "DejaVuSans-Bold.ttf",
            )
        )
    except (ImportError, OSError, ValueError):
        pass
    return candidates


def _register_fonts(required_text: str) -> tuple[str, str]:
    pair = next(
        ((regular, bold) for regular, bold in _font_candidates() if regular.is_file() and bold.is_file()),
        None,
    )
    if pair is None:
        raise PdfGenerationError(
            "Не знайдено локальний Unicode-шрифт для українського PDF."
        )
    regular_path, bold_path = pair
    regular_font = TTFont(FONT_REGULAR, str(regular_path))
    bold_font = TTFont(FONT_BOLD, str(bold_path))
    required_codepoints = {ord(character) for character in _normalize_text(required_text)}
    missing_regular = required_codepoints - set(regular_font.face.charToGlyph)
    missing_bold = required_codepoints - set(bold_font.face.charToGlyph)
    missing = sorted(missing_regular | missing_bold)
    if missing:
        codes = ", ".join(f"U+{code:04X}" for code in missing[:12])
        suffix = " ..." if len(missing) > 12 else ""
        raise PdfGenerationError(
            f"Локальний шрифт не містить потрібні гліфи: {codes}{suffix}"
        )
    if FONT_REGULAR not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(regular_font)
    if FONT_BOLD not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(bold_font)
    return FONT_REGULAR, FONT_BOLD


def _required_text(
    assessment: Assessment,
    catalog: FunctionCatalog,
    systems: list[dict[str, object]],
) -> str:
    return "\n".join(
        [
            STATIC_TEXT,
            json.dumps(assessment.model_dump(mode="json"), ensure_ascii=False, default=str),
            json.dumps(catalog.model_dump(mode="json"), ensure_ascii=False, default=str),
            json.dumps(systems, ensure_ascii=False, default=str),
        ]
    )


def safe_pdf_filename(assessment: Assessment) -> str:
    source_name = _normalize_text(assessment.project_name or "smart-home-project")
    ascii_name = unicodedata.normalize("NFKD", source_name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^A-Za-z0-9]+", "-", ascii_name).strip("-").lower()
    if not slug:
        slug = "smart-home-project"
    slug = slug[:44].rstrip("-") or "smart-home-project"
    digest = sha256(
        f"{assessment.assessment_id}|{source_name}".encode("utf-8")
    ).hexdigest()[:10]
    return f"{slug}-{digest}.pdf"


def _format_decimal(value: Decimal) -> str:
    return f"{value:,.2f}".replace(",", " ")


def _styles(regular_font: str, bold_font: str) -> dict[str, ParagraphStyle]:
    sample = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "PdfTitle",
            parent=sample["Title"],
            fontName=bold_font,
            fontSize=18,
            leading=21,
            textColor=colors.HexColor("#182028"),
            alignment=TA_LEFT,
            spaceAfter=5 * mm,
        ),
        "subtitle": ParagraphStyle(
            "PdfSubtitle",
            parent=sample["Normal"],
            fontName=regular_font,
            fontSize=8.5,
            leading=11,
            textColor=colors.HexColor("#53606B"),
            spaceAfter=3 * mm,
        ),
        "h2": ParagraphStyle(
            "PdfH2",
            parent=sample["Heading2"],
            fontName=bold_font,
            fontSize=12,
            leading=15,
            textColor=colors.HexColor("#182028"),
            spaceBefore=4 * mm,
            spaceAfter=2 * mm,
            keepWithNext=True,
        ),
        "h3": ParagraphStyle(
            "PdfH3",
            parent=sample["Heading3"],
            fontName=bold_font,
            fontSize=9,
            leading=11,
            textColor=colors.HexColor("#33404A"),
            spaceBefore=2.5 * mm,
            spaceAfter=1.5 * mm,
            keepWithNext=False,
        ),
        "body": ParagraphStyle(
            "PdfBody",
            parent=sample["BodyText"],
            fontName=regular_font,
            fontSize=8,
            leading=10.2,
            textColor=colors.HexColor("#25313B"),
        ),
        "small": ParagraphStyle(
            "PdfSmall",
            parent=sample["BodyText"],
            fontName=regular_font,
            fontSize=7,
            leading=8.6,
            textColor=colors.HexColor("#53606B"),
        ),
        "table": ParagraphStyle(
            "PdfTable",
            parent=sample["BodyText"],
            fontName=regular_font,
            fontSize=6.6,
            leading=8.1,
            textColor=colors.HexColor("#25313B"),
        ),
        "table_bold": ParagraphStyle(
            "PdfTableBold",
            parent=sample["BodyText"],
            fontName=bold_font,
            fontSize=6.6,
            leading=8.1,
            textColor=colors.HexColor("#182028"),
        ),
        "table_header": ParagraphStyle(
            "PdfTableHeader",
            parent=sample["BodyText"],
            fontName=bold_font,
            fontSize=6.5,
            leading=8,
            textColor=colors.white,
            alignment=TA_CENTER,
        ),
        "disclaimer": ParagraphStyle(
            "PdfDisclaimer",
            parent=sample["BodyText"],
            fontName=regular_font,
            fontSize=7.5,
            leading=9.5,
            textColor=colors.HexColor("#50341F"),
        ),
    }


def _paragraph(value: object, style: ParagraphStyle, *, bold: bool = False) -> Paragraph:
    normalized = escape(_normalize_text(value))
    if bold:
        normalized = f"<b>{normalized}</b>"
    return Paragraph(normalized, style)


def _base_table_style(*, header: bool = False) -> TableStyle:
    commands: list[tuple] = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 2.2 * mm),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2.2 * mm),
        ("TOPPADDING", (0, 0), (-1, -1), 1.4 * mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1.4 * mm),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#D7DEE1")),
        ("ROWBACKGROUNDS", (0, 1 if header else 0), (-1, -1), [colors.white, colors.HexColor("#F5F7F8")]),
    ]
    if header:
        commands.extend(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#33404A")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ]
        )
    return TableStyle(commands)


def _metadata_table(
    assessment: Assessment,
    styles: dict[str, ParagraphStyle],
) -> Table:
    property_label = (
        "Квартира" if assessment.property_type == PropertyType.APARTMENT else "Будинок"
    )
    config_versions = ", ".join(
        f"{key}: {json.dumps(value, ensure_ascii=False, default=str)}"
        for key, value in sorted(assessment.config_versions.items())
    ) or "Не зафіксовано"
    rows = [
        ("Назва", assessment.project_name or "Проєкт без назви"),
        ("Ідентифікатор", assessment.assessment_id),
        ("Тип житла", property_label),
        ("Площа", f"{assessment.area_m2:g} м²"),
        ("Рівні", assessment.floor_count),
        ("Ревізія", assessment.revision),
        ("Оновлено", assessment.updated_at.astimezone().strftime("%d.%m.%Y %H:%M")),
        ("Версії конфігурацій", config_versions),
    ]
    data = [
        [
            _paragraph(label, styles["table_bold"]),
            _paragraph(value, styles["table"]),
        ]
        for label, value in rows
    ]
    table = Table(data, colWidths=[42 * mm, 144 * mm], hAlign="LEFT")
    table.setStyle(_base_table_style())
    return table


def _systems_table(
    assessment: Assessment,
    systems: list[dict[str, object]],
    styles: dict[str, ParagraphStyle],
) -> LongTable:
    system_map = {str(item["system_id"]): item for item in systems}
    header = ["#", "Система", "Статус", "%", "Пояснення"]
    data = [[_paragraph(item, styles["table_header"]) for item in header]]
    for index, recommendation in enumerate(assessment.recommendations, start=1):
        system = system_map.get(recommendation.system_id, {})
        data.append(
            [
                _paragraph(index, styles["table"]),
                _paragraph(system.get("name_uk", recommendation.system_id), styles["table_bold"]),
                _paragraph(STATUS_LABELS[recommendation.status], styles["table"]),
                _paragraph(
                    recommendation.suitability_percent
                    if recommendation.suitability_percent is not None
                    else "-",
                    styles["table"],
                ),
                _paragraph(recommendation.reason, styles["table"]),
            ]
        )
    table = LongTable(
        data,
        colWidths=[8 * mm, 35 * mm, 27 * mm, 12 * mm, 104 * mm],
        repeatRows=1,
        splitByRow=1,
        hAlign="LEFT",
    )
    table.setStyle(_base_table_style(header=True))
    return table


def _functions_table(
    assessment: Assessment,
    summary: ProjectSummary,
    catalog: FunctionCatalog,
    systems: list[dict[str, object]],
    phase: ImplementationPhase,
    styles: dict[str, ParagraphStyle],
) -> LongTable | Paragraph:
    catalog_map = {item.function_id: item for item in catalog.functions}
    room_map = {item.room_id: item for item in assessment.rooms}
    room_order = {item.room_id: item.sort_order for item in assessment.rooms}
    system_map = {str(item["system_id"]): item for item in systems}
    items = [item for item in summary.scenario.items if item.selected_phase == phase]
    if not items:
        return _paragraph("У цій фазі немає вибраних функцій.", styles["small"])

    groups = group_scenario_functions(items, room_sort_order=room_order)
    header = [
        "Функція",
        "Приміщення / локації",
        "Система / статус",
        "Фази",
        "Сценарій / примітки",
    ]
    data = [[_paragraph(item, styles["table_header"]) for item in header]]
    for group in groups:
        function = catalog_map[group.function_id]
        notes = []
        if group.is_prerequisite:
            notes.append("передумова")
        if group.needs_review:
            notes.append("потребує перевірки")
        if group.preparation_required:
            notes.append("передбачити підготовку")
        focus_and_notes = " / ".join(
            FOCUS_LABELS[focus_state] for focus_state in group.focus_states
        )
        if notes:
            focus_and_notes += "; " + ", ".join(notes)
        status_text = " / ".join(
            STATUS_LABELS[status] for status in group.recommendation_statuses
        )
        recommended_phase_text = " / ".join(
            PHASE_LABELS[item_phase] for item_phase in group.recommended_phases
        )
        selected_phase_text = " / ".join(
            PHASE_LABELS[item_phase] for item_phase in group.selected_phases
        )
        data.append(
            [
                _paragraph(function.name_uk, styles["table_bold"]),
                _paragraph(
                    ", ".join(room_map[room_id].name for room_id in group.room_ids),
                    styles["table"],
                ),
                _paragraph(
                    f"{system_map[group.system_id].get('name_uk', group.system_id)}; "
                    f"{status_text}",
                    styles["table"],
                ),
                _paragraph(
                    f"Рекомендована: {recommended_phase_text}; "
                    f"вибрана: {selected_phase_text}",
                    styles["table"],
                ),
                _paragraph(focus_and_notes, styles["table"]),
            ]
        )
    table = LongTable(
        data,
        colWidths=[40 * mm, 29 * mm, 39 * mm, 41 * mm, 37 * mm],
        repeatRows=1,
        splitByRow=1,
        hAlign="LEFT",
    )
    table.setStyle(_base_table_style(header=True))
    return table


def _audit_story(
    assessment: Assessment,
    summary: ProjectSummary,
    catalog: FunctionCatalog,
    styles: dict[str, ParagraphStyle],
) -> list[object]:
    catalog_map = {item.function_id: item for item in catalog.functions}
    room_map = {item.room_id: item for item in assessment.rooms}
    review_items = [item for item in assessment.function_recommendations if item.needs_review]
    parts: list[object] = [
        _paragraph("Перевірка даних і залежностей", styles["h2"]),
        _paragraph(
            f"Позначено для підтвердження: {len(review_items)}. "
            f"Prerequisite-зв'язків у вибраному обсязі: {summary.dependency_links}. "
            f"Невиконаних залежностей: {summary.unmet_dependencies}.",
            styles["body"],
        ),
    ]
    if review_items:
        review_rows = [[
            _paragraph("Функція", styles["table_header"]),
            _paragraph("Локація", styles["table_header"]),
            _paragraph("Причина", styles["table_header"]),
        ]]
        grouped_review: dict[tuple[str, str], list[str]] = {}
        for item in review_items:
            grouped_review.setdefault((item.function_id, item.reason), []).append(
                room_map[item.room_id].name
            )
        for (function_id, reason), locations in grouped_review.items():
            review_rows.append(
                [
                    _paragraph(catalog_map[function_id].name_uk, styles["table_bold"]),
                    _paragraph(", ".join(dict.fromkeys(locations)), styles["table"]),
                    _paragraph(reason, styles["table"]),
                ]
            )
        table = LongTable(
            review_rows,
            colWidths=[48 * mm, 38 * mm, 100 * mm],
            repeatRows=1,
            splitByRow=1,
            hAlign="LEFT",
        )
        table.setStyle(_base_table_style(header=True))
        parts.extend([Spacer(1, 2 * mm), table])
    return parts


def _budget_story(
    assessment: Assessment,
    summary: ProjectSummary,
    styles: dict[str, ParagraphStyle],
) -> list[object]:
    if assessment.budget is None:
        raise PdfGenerationError("Бюджетна позиція ще не збережена.")
    amount = (
        f"{_format_decimal(assessment.budget.amount_eur)} EUR"
        if assessment.budget.amount_eur is not None
        else "Суму не вказано"
    )
    current_focus_count = sum(
        item.focus_state == "current_focus" for item in summary.scenario.items
    )
    next_count = sum(item.focus_state == "next_stage" for item in summary.scenario.items)
    later_count = sum(
        item.focus_state == "later_or_optional" for item in summary.scenario.items
    )
    focus_text = (
        f"Поточний фокус: {current_focus_count}; наступний етап: {next_count}; "
        f"пізніше або за бажанням: {later_count}. Бюджет не змінював статуси, "
        "рекомендовані фази або фази, вибрані користувачем."
        if summary.selected_total else
        "До поточного плану не включено функцій, тому бюджетна позиція не "
        "розподіляє їх за етапами й не змінює оцінку систем."
    )
    return [
        _paragraph("Бюджетна позиція", styles["h2"]),
        _paragraph(
            f"{BUDGET_LABELS[assessment.budget.level]}. "
            f"{BUDGET_DESCRIPTIONS[assessment.budget.level]} "
            f"Заявлений користувачем орієнтир: {amount}.",
            styles["body"],
        ),
        Spacer(1, 1.5 * mm),
        _paragraph(focus_text, styles["small"]),
    ]


def build_project_pdf(
    assessment: Assessment,
    catalog: FunctionCatalog,
    systems: list[dict[str, object]],
) -> bytes:
    """Build a side-effect-free PDF from the current immutable project snapshot."""
    if len(assessment.recommendations) != 19:
        raise PdfGenerationError("PDF потребує повного набору з 19 систем.")
    try:
        summary = build_project_summary(assessment, catalog)
    except (KeyError, StopIteration, ValueError) as error:
        raise PdfGenerationError(f"Неможливо сформувати підсумок PDF: {error}") from error

    regular_font, bold_font = _register_fonts(_required_text(assessment, catalog, systems))
    styles = _styles(regular_font, bold_font)
    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=12 * mm,
        rightMargin=12 * mm,
        topMargin=15 * mm,
        bottomMargin=14 * mm,
        title="Попередній проєкт розумного дому",
        author="Рекомендаційна система розумного дому",
        subject="Попередній планувальний документ",
    )

    story: list[object] = [
        _paragraph("Попередній проєкт розумного дому", styles["title"]),
        _paragraph(
            "Персоналізований підсумок оцінки систем і попереднього плану "
            "реалізації.",
            styles["subtitle"],
        ),
        _paragraph("Метадані проєкту", styles["h2"]),
        _metadata_table(assessment, styles),
        _paragraph("Оцінка систем", styles["h2"]),
        _paragraph(
            "Відсоток означає доцільність для профілю користувача, а не "
            "ймовірність, точну економію або оцінку вартості.",
            styles["small"],
        ),
    ]
    explanation_status = assessment.config_versions.get("explanation_status")
    if explanation_status == "initial_llm_unverified":
        story.append(_paragraph(
            "Увага: фінальні пояснення LLM не пройшли перевірку. Наведено "
            "початкові формулювання моделі, які потрібно звірити з профілем.",
            styles["small"],
        ))
    elif explanation_status == "partial_llm_validated":
        unverified_ids = set(
            assessment.config_versions.get("unverified_explanation_system_ids", [])
        )
        system_names = {
            str(item["system_id"]): str(item["name_uk"]) for item in systems
        }
        names = ", ".join(
            system_names.get(item.system_id, item.system_id)
            for item in assessment.recommendations
            if item.system_id in unverified_ids
        )
        story.append(_paragraph(
            "Увага: пояснення LLM потребують перевірки за відповідями анкети: "
            + names + ".",
            styles["small"],
        ))
    elif explanation_status == "rule_only_fallback":
        story.append(_paragraph(
            "Хмарна модель була недоступна: пояснення сформовано лише правилами.",
            styles["small"],
        ))
    story.extend([
        Spacer(1, 1.5 * mm),
        _systems_table(assessment, systems, styles),
        CondPageBreak(30 * mm),
        _paragraph(
            "Вибрані функції" if summary.selected_total else
            "Рекомендований функціональний обсяг",
            styles["h2"],
        ),
        _paragraph(
            (
                f"Вибрано: {summary.selected_total}; не вибрано: "
                f"{summary.not_selected_count}; потребують перевірки: "
                f"{summary.needs_review_count}."
                if summary.selected_total else
                f"Автоматично включено: 0; поза поточним обсягом: "
                f"{summary.not_selected_count}."
            ),
            styles["body"],
        ),
    ])
    if summary.selected_total == 0:
        message, optional_text = empty_scope_guidance(assessment, systems)
        story.append(_paragraph(message, styles["body"]))
        if optional_text:
            story.append(_paragraph(optional_text, styles["body"]))
            system_names = {
                str(item["system_id"]): str(item["name_uk"]) for item in systems
            }
            for item in assessment.recommendations:
                if item.status != RecommendationStatus.OPTIONAL:
                    continue
                story.extend([
                    CondPageBreak(24 * mm),
                    _paragraph(
                        f"{system_names.get(item.system_id, item.system_id)} "
                        f"({item.suitability_percent}%)",
                        styles["h3"],
                    ),
                    _paragraph(item.reason, styles["body"]),
                ])
    else:
        for phase in ImplementationPhase:
            phase_items = [
                item for item in summary.scenario.items if item.selected_phase == phase
            ]
            phase_function_count = len({item.function_id for item in phase_items})
            story.extend(
                [
                    CondPageBreak(32 * mm),
                    _paragraph(
                        f"{PHASE_LABELS[phase]} - {phase_function_count} функцій, "
                        f"{len(phase_items)} застосувань",
                        styles["h3"],
                    ),
                    _functions_table(assessment, summary, catalog, systems, phase, styles),
                ]
            )
        story.extend(_audit_story(assessment, summary, catalog, styles))
    story.extend(_budget_story(assessment, summary, styles))
    story.extend(
        [
            _paragraph("Інженерні застереження", styles["h2"]),
            Table(
                [[
                    _paragraph(
                        "Це попередній планувальний документ, а не проєкт, "
                        "специфікація, комерційна пропозиція або кошторис. "
                        "Бюджет не змінював рекомендації та фази і не гарантує, "
                        "що вибраний обсяг вкладається у вказану суму. Обладнання, "
                        "роботи, кабелі, сумісність, навантаження і розміщення "
                        "визначає інтегратор після обстеження об'єкта.",
                        styles["disclaimer"],
                    )
                ]],
                colWidths=[186 * mm],
                style=TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FFF4E8")),
                        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#D58A46")),
                        ("LEFTPADDING", (0, 0), (-1, -1), 4 * mm),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 4 * mm),
                        ("TOPPADDING", (0, 0), (-1, -1), 3 * mm),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 3 * mm),
                    ]
                ),
            ),
        ]
    )

    project_footer = _normalize_text(assessment.project_name or "Проєкт розумного дому")

    def draw_footer(canvas, doc) -> None:
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor("#D7DEE1"))
        canvas.setLineWidth(0.4)
        canvas.line(12 * mm, 10 * mm, A4[0] - 12 * mm, 10 * mm)
        canvas.setFont(regular_font, 6.8)
        canvas.setFillColor(colors.HexColor("#68737D"))
        canvas.drawString(12 * mm, 6.5 * mm, project_footer[:90])
        canvas.drawRightString(
            A4[0] - 12 * mm,
            6.5 * mm,
            f"Сторінка {doc.page}",
        )
        canvas.restoreState()

    try:
        document.build(story, onFirstPage=draw_footer, onLaterPages=draw_footer)
    except Exception as error:
        raise PdfGenerationError(f"Не вдалося побудувати PDF: {error}") from error
    payload = buffer.getvalue()
    if not payload.startswith(b"%PDF-"):
        raise PdfGenerationError("Генератор повернув некоректний PDF.")
    return payload
