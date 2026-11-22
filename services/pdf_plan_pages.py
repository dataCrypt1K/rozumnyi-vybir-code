"""Inspect and render selected PDF pages without changing the original file."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import re

import pypdfium2 as pdfium


MAX_ANALYSIS_PAGES = 8
_PAGE_SIGNALS = (
    ("експлікац", 6),
    ("план першого поверх", 5),
    ("план другого поверх", 5),
    ("план 1 поверх", 5),
    ("план 2 поверх", 5),
    ("обмірн", 4),
    ("план приміщ", 4),
    ("плануван", 3),
    ("сантех", 2),
    ("водопостач", 2),
    ("опален", 2),
    ("вентиляц", 2),
    ("кондиціюван", 2),
    ("електр", 1),
    ("генплан", 1),
    ("room schedule", 6),
    ("floor plan", 4),
)
_FLOOR_TITLE_PATTERNS = {
    1: (
        r"\bплан\b.{0,35}\b(?:першого|1-?(?:ий|й|го))\s+поверх",
        r"\bfirst\s+floor\s+plan\b",
        r"\bfloor\s+plan\s+(?:1|first)\b",
    ),
    2: (
        r"\bплан\b.{0,35}\b(?:другого|2-?(?:ий|й|го))\s+поверх",
        r"\bsecond\s+floor\s+plan\b",
        r"\bfloor\s+plan\s+(?:2|second)\b",
    ),
}
_OTHER_LEVELS = re.compile(
    r"цоколь|підвал|мансард|третього\s+поверх|3-?(?:ій|й|го)\s+поверх|"
    r"basement|attic|third\s+floor",
)
_PAGE_ROLE_PRIORITY = {
    "other": 0,
    "room_schedule": 1,
    "layout": 2,
    "measurement": 3,
    "floor_plan": 4,
    "final_layout": 5,
}


@dataclass(frozen=True)
class PdfPagePreview:
    number: int
    score: int
    label: str
    thumbnail_png: bytes
    plan_role: str = "other"
    floor_number: int | None = None
    text_length: int = 0


def _page_selection_metadata(page_text: str) -> tuple[str, int | None]:
    text = re.sub(r"\s+", " ", page_text.casefold())
    if re.search(
        r"відомість креслень комплекту|перелік креслень|"
        r"drawing index|sheet index|table of contents",
        text,
    ):
        return "other", None
    floor_titles = [
        level for level, patterns in _FLOOR_TITLE_PATTERNS.items()
        if any(re.search(pattern, text) for pattern in patterns)
    ]
    floor_number = floor_titles[0] if len(floor_titles) == 1 else None
    if re.search(
        r"план після перепланування|фінальне планування|"
        r"остаточне планування|final floor plan",
        text,
    ):
        return "final_layout", floor_number
    if re.search(
        r"\bплан\s+(?:першого|другого|1-?(?:ий|й|го)|2-?(?:ий|й|го))\s+поверх|"
        r"\b(?:first|second)\s+floor\s+plan\b",
        text,
    ):
        return "floor_plan", floor_number
    if "обмірний план" in text or "план обмірів" in text:
        return "measurement", floor_number
    if "план будинку" in text and "відомість приміщень" in text:
        return "layout", floor_number
    if any(mark in text for mark in (
        "експлікація приміщень", "відомість приміщень", "room schedule",
    )):
        return "room_schedule", floor_number
    return "other", floor_number


def _render_png(page: pdfium.PdfPage, *, max_edge: int) -> bytes:
    scale = min(2.5, max_edge / max(page.get_width(), page.get_height()))
    bitmap = page.render(scale=scale)
    try:
        image = bitmap.to_pil()
        output = BytesIO()
        image.save(output, format="PNG", optimize=True)
        return output.getvalue()
    finally:
        bitmap.close()


def inspect_pdf_pages(data: bytes) -> list[PdfPagePreview]:
    try:
        document = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as error:
        raise ValueError("Не вдалося відкрити PDF. Перевірте файл.") from error
    previews = []
    try:
        for index in range(len(document)):
            page = document[index]
            try:
                text_page = page.get_textpage()
                try:
                    page_text = text_page.get_text_range().casefold()
                finally:
                    text_page.close()
                signals = [
                    (label, weight) for label, weight in _PAGE_SIGNALS
                    if label in page_text
                ]
                area_marks = sum(page_text.count(mark) for mark in ("м²", "m²", "м2", "m2"))
                if area_marks >= 5:
                    signals.append(("Площі приміщень", 7))
                score = sum(weight for _, weight in signals)
                label = max(signals, key=lambda item: item[1])[0] if signals else "план"
                plan_role, floor_number = _page_selection_metadata(page_text)
                previews.append(PdfPagePreview(
                    number=index + 1,
                    score=score,
                    label=label.capitalize(),
                    thumbnail_png=_render_png(page, max_edge=440),
                    plan_role=plan_role,
                    floor_number=floor_number,
                    text_length=len(page_text.strip()),
                ))
            finally:
                page.close()
    except pdfium.PdfiumError as error:
        raise ValueError("Не вдалося прочитати сторінки PDF. Перевірте файл.") from error
    finally:
        document.close()
    return previews


def suggested_pdf_pages(previews: list[PdfPagePreview]) -> list[int]:
    if not previews:
        return []
    rank = lambda page: (_PAGE_ROLE_PRIORITY[page.plan_role], page.score, -page.number)
    best_role = max(_PAGE_ROLE_PRIORITY[page.plan_role] for page in previews)
    if best_role == 0:
        # Image-only PDFs may contain several 3D views of the same apartment.
        return [max(previews, key=lambda page: (page.score, -page.number)).number]
    candidates = [
        page for page in previews
        if _PAGE_ROLE_PRIORITY[page.plan_role] == best_role
    ]
    floor_pages = [page for page in candidates if page.floor_number is not None]
    if floor_pages:
        return sorted(
            max(
                (page for page in floor_pages if page.floor_number == floor),
                key=rank,
            ).number
            for floor in sorted({page.floor_number for page in floor_pages})
        )
    return [max(candidates, key=rank).number]


def pdf_page_is_floor_specific(data: bytes, page_number: int) -> bool:
    try:
        document = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as error:
        raise ValueError("Не вдалося відкрити PDF. Перевірте файл.") from error
    try:
        if not 1 <= page_number <= len(document):
            raise ValueError("Номер сторінки PDF некоректний.")
        page = document[page_number - 1]
        try:
            text_page = page.get_textpage()
            try:
                page_text = text_page.get_text_range().casefold()
            finally:
                text_page.close()
        finally:
            page.close()
    finally:
        document.close()
    return _page_selection_metadata(page_text)[1] is not None


def pdf_known_floor_count(data: bytes, selected_pages: list[int]) -> tuple[int, list[int]] | None:
    """Use explicit plan titles only when the PDF contains no sign of another level."""
    try:
        document = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as error:
        raise ValueError("Не вдалося відкрити PDF. Перевірте файл.") from error
    title_pages: dict[int, list[int]] = {1: [], 2: []}
    other_level = False
    try:
        for index in range(len(document)):
            page = document[index]
            try:
                text_page = page.get_textpage()
                try:
                    text = re.sub(r"\s+", " ", text_page.get_text_range().casefold())
                finally:
                    text_page.close()
            finally:
                page.close()
            other_level |= bool(_OTHER_LEVELS.search(text))
            for level, patterns in _FLOOR_TITLE_PATTERNS.items():
                if any(re.search(pattern, text) for pattern in patterns):
                    title_pages[level].append(index + 1)
    finally:
        document.close()
    selected = set(selected_pages)
    if other_level:
        return None
    first_pages = [page for page in title_pages[1] if page in selected]
    second_pages = [page for page in title_pages[2] if page in selected]
    if not first_pages or not second_pages:
        return None
    exclusive_first = [page for page in first_pages if page not in second_pages]
    exclusive_second = [page for page in second_pages if page not in first_pages]
    for first in exclusive_first or first_pages:
        for second in exclusive_second or second_pages:
            if first != second:
                return 2, [first, second]
    return None


def render_selected_pdf_pages(data: bytes, page_numbers: list[int]) -> list[tuple[int, bytes]]:
    try:
        document = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as error:
        raise ValueError("Не вдалося відкрити PDF. Перевірте файл.") from error
    try:
        if not page_numbers or len(page_numbers) > MAX_ANALYSIS_PAGES:
            raise ValueError(f"Оберіть від 1 до {MAX_ANALYSIS_PAGES} сторінок PDF.")
        if any(
            type(number) is not int or number < 1 or number > len(document)
            for number in page_numbers
        ) or len(page_numbers) != len(set(page_numbers)):
            raise ValueError("Номери сторінок PDF некоректні.")
        rendered = []
        for number in sorted(page_numbers):
            page = document[number - 1]
            try:
                rendered.append((number, _render_png(page, max_edge=2400)))
            finally:
                page.close()
        return rendered
    except pdfium.PdfiumError as error:
        raise ValueError("Не вдалося відкрити вибрані сторінки PDF.") from error
    finally:
        document.close()
