import csv
import io
import re
import zipfile
import shutil
import concurrent.futures
from pathlib import Path

import chardet
import olefile
import xlrd
from docx import Document
from openpyxl import load_workbook
from PIL import Image
from pypdf import PdfReader
import pytesseract
from pdf2image import convert_from_bytes


ALLOWED_EXTENSIONS = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".csv": "text/csv",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


class DocumentProcessingError(Exception):
    pass


class DocumentProcessingUnavailableError(DocumentProcessingError):
    pass


def validate_file(filename: str, content: bytes) -> str:
    name = Path(filename).name
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise DocumentProcessingError("This file type is not supported.")
    if not content:
        raise DocumentProcessingError("The uploaded file is empty.")

    pdf = content.startswith(b"%PDF")
    doc = content.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
    zip_based = content.startswith(b"PK\x03\x04")
    image = False
    try:
        with Image.open(io.BytesIO(content)) as image_file:
            image_file.verify()
            image = True
    except Exception:
        image = False

    if suffix == ".pdf" and not pdf:
        raise DocumentProcessingError("The PDF content does not match a valid PDF file.")
    if suffix == ".doc" and not doc:
        raise DocumentProcessingError("The DOC content does not match a valid legacy Word file.")
    if suffix in {".docx", ".xlsx"} and not zip_based:
        raise DocumentProcessingError("The office document content is invalid.")
    if suffix in {".xls"} and not doc:
        raise DocumentProcessingError("The XLS content does not match a valid Excel file.")
    if suffix in {".png", ".jpg", ".jpeg", ".webp"} and not image:
        raise DocumentProcessingError("The image content is invalid.")
    if suffix == ".csv":
        text = content[:4096].decode("utf-8", "replace")
        if "\x00" in text:
            raise DocumentProcessingError("The CSV content is invalid.")
    if suffix == ".txt" and b"\x00" in content[:4096]:
        raise DocumentProcessingError("The TXT content is invalid.")
    return suffix


def _decode_text(content: bytes) -> str:
    detected = chardet.detect(content)
    encoding = detected.get("encoding") or "utf-8"
    try:
        return content.decode(encoding, "replace")
    except LookupError:
        return content.decode("utf-8", "replace")


def _ocr_image(
    image: Image.Image,
    tesseract_cmd: str | None,
    language: str,
    timeout_seconds: int,
) -> dict:
    if tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
    try:
        data = pytesseract.image_to_data(
            image,
            lang=language,
            timeout=timeout_seconds,
            output_type=pytesseract.Output.DICT,
        )
        text = pytesseract.image_to_string(
            image,
            lang=language,
            timeout=timeout_seconds,
        ).strip()
    except (pytesseract.TesseractNotFoundError, pytesseract.TesseractError) as exc:
        raise DocumentProcessingUnavailableError(
            "OCR is unavailable. Install Tesseract OCR and configure TESSERACT_CMD."
        ) from exc
    except Exception as exc:
        raise DocumentProcessingError("OCR could not process this image.") from exc

    confidences = [
        float(confidence)
        for confidence in data.get("conf", [])
        if str(confidence).replace("-", "").replace(".", "").isdigit()
        and float(confidence) >= 0
    ]
    average_confidence = (
        sum(confidences) / len(confidences)
        if confidences
        else None
    )
    return {
        "text": text,
        "confidence": average_confidence,
    }


def extract_text(
    filename: str,
    content: bytes,
    *,
    ocr_enabled: bool = True,
    tesseract_cmd: str | None = None,
    poppler_path: str | None = None,
    ocr_language: str = "eng",
    ocr_timeout_seconds: int = 30,
    ocr_max_image_pixels: int = 25_000_000,
    ocr_max_pdf_pages: int = 30,
) -> list[dict]:
    suffix = validate_file(filename, content)

    if suffix == ".pdf":
        reader = PdfReader(io.BytesIO(content))
        sections = []
        for page_number, page in enumerate(reader.pages, start=1):
            text = page.extract_text() or ""
            sections.append({
                "text": text,
                "source": f"Page {page_number}",
            })
        if not sections or not any(section["text"].strip() for section in sections):
            if not ocr_enabled:
                return [{
                    "text": "No extractable text was found and OCR is disabled.",
                    "source": "PDF",
                }]
            if len(reader.pages) > ocr_max_pdf_pages:
                raise DocumentProcessingError(
                    f"The scanned PDF exceeds the configured page limit of {ocr_max_pdf_pages}."
                )
            if shutil.which("tesseract") is None and not tesseract_cmd:
                raise DocumentProcessingUnavailableError(
                    "Scanned PDF OCR is unavailable. Install Tesseract OCR."
                )
            try:
                pages = convert_from_bytes(
                    content,
                    dpi=200,
                    fmt="png",
                    poppler_path=poppler_path,
                    timeout=ocr_timeout_seconds,
                )
            except Exception as exc:
                raise DocumentProcessingError(
                    "The scanned PDF could not be rendered. Install Poppler and configure POPPLER_PATH."
                ) from exc

            scanned_sections = []
            for page_number, page in enumerate(pages[:ocr_max_pdf_pages], start=1):
                width, height = page.size
                if width * height > ocr_max_image_pixels:
                    page.thumbnail(
                        (
                            int((ocr_max_image_pixels ** 0.5)),
                            int((ocr_max_image_pixels ** 0.5)),
                        )
                    )
                ocr = _ocr_image(
                    page,
                    tesseract_cmd,
                    ocr_language,
                    ocr_timeout_seconds,
                )
                if ocr["text"]:
                    confidence = ocr["confidence"]
                    confidence_text = (
                        f" OCR confidence: {confidence:.1f}%."
                        if confidence is not None
                        else ""
                    )
                    scanned_sections.append({
                        "text": ocr["text"] + confidence_text,
                        "source": f"Page {page_number} (OCR)",
                    })
            if not scanned_sections:
                raise DocumentProcessingError(
                    "OCR completed but no usable text was found in the scanned PDF."
                )
            return scanned_sections
        return sections

    if suffix == ".docx":
        document = Document(io.BytesIO(content))
        sections = []
        if document.paragraphs:
            paragraph_text = "\n".join(
                paragraph.text.strip()
                for paragraph in document.paragraphs
                if paragraph.text.strip()
            )
            if paragraph_text:
                sections.append({"text": paragraph_text, "source": "Paragraphs"})
        for table_number, table in enumerate(document.tables, start=1):
            rows = [
                " | ".join(cell.text.strip().replace("\n", " ") for cell in row.cells)
                for row in table.rows
            ]
            if rows:
                sections.append({
                    "text": "\n".join(rows),
                    "source": f"Table {table_number}",
                })
        return sections or [{"text": "", "source": "DOCX"}]

    if suffix == ".doc":
        ole = olefile.OleFileIO(io.BytesIO(content))
        if not ole.exists("WordDocument"):
            raise DocumentProcessingError("The legacy DOC file does not contain a WordDocument stream.")
        stream = ole.openstream("WordDocument").read()
        matches = re.findall(rb"[ -~\t\r\n]{4,}|(?:[\x20-\x7e]\x00){4,}", stream)
        text_parts = []
        for match in matches:
            if b"\x00" in match:
                try:
                    text_parts.append(match.decode("utf-16-le", "ignore"))
                except UnicodeDecodeError:
                    continue
            else:
                text_parts.append(match.decode("ascii", "ignore"))
        return [{"text": "\n".join(text_parts).strip(), "source": "Legacy DOC"}]

    if suffix == ".xlsx":
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        sections = []
        for worksheet in workbook.worksheets:
            rows = []
            for row in worksheet.iter_rows(values_only=True):
                values = ["" if value is None else str(value) for value in row]
                rows.append(" | ".join(values))
            sections.append({
                "text": "\n".join(rows),
                "source": f"Sheet: {worksheet.title}",
            })
        workbook.close()
        return sections

    if suffix == ".xls":
        workbook = xlrd.open_workbook(file_contents=content)
        sections = []
        for sheet in workbook.sheets():
            rows = []
            for row_number in range(sheet.nrows):
                values = [str(sheet.cell_value(row_number, column)) for column in range(sheet.ncols)]
                rows.append(" | ".join(values))
            sections.append({
                "text": "\n".join(rows),
                "source": f"Sheet: {sheet.name}",
            })
        return sections

    if suffix == ".csv":
        text = _decode_text(content)
        reader = csv.reader(io.StringIO(text))
        rows = [" | ".join(row) for row in reader]
        return [{"text": "\n".join(rows), "source": "CSV"}]

    if suffix == ".txt":
        text = _decode_text(content)
        return [{"text": text, "source": "Text"}]

    if suffix in {".png", ".jpg", ".jpeg", ".webp"}:
        if not ocr_enabled:
            return [{
                "text": "Image uploaded successfully. OCR is disabled.",
                "source": "Image metadata",
            }]
        try:
            with Image.open(io.BytesIO(content)) as image:
                image.load()
                width, height = image.size
                if width * height > ocr_max_image_pixels:
                    raise DocumentProcessingError(
                        "The image exceeds the configured pixel limit."
                    )
                if shutil.which("tesseract") is None and not tesseract_cmd:
                    raise DocumentProcessingUnavailableError(
                        "OCR is unavailable. Install Tesseract OCR."
                    )
                ocr = _ocr_image(
                    image.copy(),
                    tesseract_cmd,
                    ocr_language,
                    ocr_timeout_seconds,
                )
        except DocumentProcessingError:
            raise
        except Exception as exc:
            raise DocumentProcessingError("The image could not be processed.") from exc

        if not ocr["text"]:
            raise DocumentProcessingError("OCR found no usable text in the image.")
        confidence = ocr["confidence"]
        confidence_text = (
            f"\nOCR confidence: {confidence:.1f}%"
            if confidence is not None
            else ""
        )
        return [{
            "text": ocr["text"] + confidence_text,
            "source": "Image OCR",
        }]

    return []


def chunk_sections(sections: list[dict], chunk_chars: int) -> list[dict]:
    chunks = []
    for section in sections:
        text = section["text"].strip()
        if not text:
            continue
        for start in range(0, len(text), chunk_chars):
            chunks.append({
                "text": text[start:start + chunk_chars],
                "source": section["source"],
            })
    return chunks


def retrieve_chunks(
    chunks: list[dict],
    query: str,
    limit: int,
) -> list[dict]:
    terms = [term.lower() for term in re.findall(r"[A-Za-z0-9_]+", query) if len(term) > 1]
    if not terms:
        return chunks[:limit]

    ranked = []
    for index, chunk in enumerate(chunks):
        text = chunk["text"].lower()
        score = sum(text.count(term) for term in terms)
        ranked.append((score, -index, chunk))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[2] for item in ranked[:limit] if item[0] > 0] or chunks[:limit]
