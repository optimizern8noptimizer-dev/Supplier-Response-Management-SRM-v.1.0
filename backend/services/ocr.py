"""
OCR Service
Извлечение текста из PDF (цифровых и отсканированных).
"""
import os
import logging
import tempfile
from pathlib import Path

import pytesseract
from PIL import Image
from pdf2image import convert_from_path
from pypdf import PdfReader

logger = logging.getLogger(__name__)

# Tesseract: русский + английский
TESSERACT_LANG = "rus+eng"
TESSERACT_CONFIG = "--oem 3 --psm 3"  # LSTM + Auto page segmentation


def extract_text_from_pdf(file_path: str) -> str:
    """
    Извлекает текст из PDF.
    Стратегия:
      1. Пробует цифровое извлечение (pypdf) — быстро, точно
      2. Если текста нет или мало — OCR через Tesseract (для сканов)
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Файл не найден: {file_path}")

    # Попытка 1: цифровой PDF
    digital_text = _extract_digital_pdf(file_path)
    if len(digital_text.strip()) > 100:
        logger.info(f"[OCR] Цифровое извлечение: {len(digital_text)} символов из {path.name}")
        return digital_text.strip()

    # Попытка 2: OCR для сканов
    logger.info(f"[OCR] Текст не найден цифровым методом, запускаю Tesseract OCR для {path.name}")
    ocr_text = _extract_scanned_pdf(file_path)
    logger.info(f"[OCR] OCR извлечено: {len(ocr_text)} символов")
    return ocr_text.strip()


def _extract_digital_pdf(file_path: str) -> str:
    """Извлечение текста из цифрового PDF через pypdf."""
    try:
        reader = PdfReader(file_path)
        pages_text = []
        for i, page in enumerate(reader.pages):
            text = page.extract_text()
            if text:
                pages_text.append(f"[Страница {i+1}]\n{text}")
        return "\n\n".join(pages_text)
    except Exception as e:
        logger.warning(f"[OCR] pypdf ошибка: {e}")
        return ""


def _extract_scanned_pdf(file_path: str) -> str:
    """OCR через Tesseract — конвертация PDF в изображения."""
    pages_text = []
    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            images = convert_from_path(
                file_path,
                dpi=300,           # Высокое DPI для качественного OCR
                output_folder=tmpdir,
                fmt="png",
                thread_count=2,
            )
        except Exception as e:
            logger.error(f"[OCR] pdf2image ошибка: {e}")
            return ""

        for i, image in enumerate(images):
            try:
                text = pytesseract.image_to_string(
                    image,
                    lang=TESSERACT_LANG,
                    config=TESSERACT_CONFIG
                )
                if text.strip():
                    pages_text.append(f"[Страница {i+1}]\n{text}")
            except Exception as e:
                logger.warning(f"[OCR] Ошибка OCR страницы {i+1}: {e}")

    return "\n\n".join(pages_text)


def extract_text_from_image(file_path: str) -> str:
    """OCR для отдельного изображения (JPG, PNG, TIFF)."""
    try:
        image = Image.open(file_path)
        text = pytesseract.image_to_string(
            image,
            lang=TESSERACT_LANG,
            config=TESSERACT_CONFIG
        )
        return text.strip()
    except Exception as e:
        logger.error(f"[OCR] Ошибка OCR изображения {file_path}: {e}")
        return ""


def extract_text(file_path: str) -> str:
    """
    Универсальная функция: определяет тип файла и вызывает нужный метод.
    Поддерживаемые форматы: PDF, JPG, JPEG, PNG, TIFF, BMP
    """
    ext = Path(file_path).suffix.lower()

    if ext == ".pdf":
        return extract_text_from_pdf(file_path)
    elif ext in {".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp"}:
        return extract_text_from_image(file_path)
    elif ext == ".txt":
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    else:
        raise ValueError(f"Неподдерживаемый формат файла: {ext}")


def extract_claim_number_from_text(text: str) -> str | None:
    """
    Пытается извлечь номер претензии из текста ответа поставщика.
    Ищет паттерны типа: "претензии №103827", "претензия N 103827", "claim 103827"
    """
    import re
    patterns = [
        r'претензи[иею][^0-9]*[№#N]\s*([0-9]+)',
        r'[№#N]\s*([0-9]{4,8})',
        r'претензи[иею]\s+([0-9]{4,8})',
        r'claim\s+[#№]?\s*([0-9]{4,8})',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)
    return None
