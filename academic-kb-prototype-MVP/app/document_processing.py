"""
Подготовка и загрузка документов (раздел 3.6.1-3.6.3 отчёта).

Реализует первые три этапа RAG-конвейера:
  1. Извлечение текста из PDF/DOCX/TXT.
  2. Предварительная обработка (нормализация пробелов).
  3. Разбиение на фрагменты (чанкинг) с сохранением метаданных о
     происхождении каждого фрагмента.

Не реализовано (сознательно, как заявлено в README): OCR для сканированных
PDF — для этого потребуется отдельная зависимость (например, pytesseract +
Tesseract OCR), которая не ставится через pip install без системного
бинарника; добавляется как точка расширения этой же функции extract_text.
"""
import re
from pathlib import Path
from typing import List

from pypdf import PdfReader
from docx import Document as DocxDocument


def extract_text(file_path: Path, filename: str) -> str:
    """Извлекает текстовый слой документа по расширению файла."""
    suffix = Path(filename).suffix.lower()

    if suffix == ".pdf":
        reader = PdfReader(str(file_path))
        pages = [page.extract_text() or "" for page in reader.pages]
        if not any(pages):
            raise ValueError(
                "Текстовый слой не найден (вероятно, это скан). "
                "OCR в прототипе не реализован — см. app/document_processing.py"
            )
        return "\n".join(pages)

    if suffix == ".docx":
        doc = DocxDocument(str(file_path))
        return "\n".join(p.text for p in doc.paragraphs)

    if suffix in (".txt", ".md"):
        return file_path.read_text(encoding="utf-8", errors="ignore")

    raise ValueError(f"Неподдерживаемый формат файла: {suffix}")


def clean_text(text: str) -> str:
    """Предварительная обработка: нормализация пробелов и переносов строк."""
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def chunk_text(text: str, chunk_size: int = 1000, overlap: int = 150) -> List[str]:
    """Разбивает текст на фрагменты фиксированного размера с перекрытием,
    стараясь не рвать фрагмент посреди предложения (режет по ближайшей
    точке/переносу строки внутри окна)."""
    if chunk_size <= overlap:
        raise ValueError("chunk_size должен быть больше overlap")

    chunks: List[str] = []
    start = 0
    n = len(text)

    while start < n:
        end = min(start + chunk_size, n)
        if end < n:
            window = text[start:end]
            cut = max(window.rfind(". "), window.rfind("\n"))
            if cut > chunk_size * 0.5:  # не резать слишком короткими кусками
                end = start + cut + 1
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)

        if end >= n:
            break  # дошли до конца текста — не создаём лишние перекрывающиеся хвосты

        start = max(end - overlap, start + 1)
        # перекрытие не должно начинаться посреди слова — сдвигаем к ближайшему пробелу
        gap = text.find(" ", start, start + 40)
        if gap != -1 and start > 0 and not text[start - 1].isspace():
            start = gap + 1

    return chunks
