"""
Загрузка полного текста открытых (open access) статей.

До этого чат отвечал по аннотациям. Для многих статей OpenAlex знает ссылку
на открытый PDF; здесь он скачивается, из него извлекается текст (pypdf, чистый
Python — без компилируемых зависимостей), режется на фрагменты и сохраняется в
служебной таблице document_chunk. Дальше полный текст участвует в поиске, а в
LLM попадают именно совпавшие с вопросом фрагменты.

Ограничения (сделаны намеренно):
- только то, что OpenAlex помечает как открытый доступ; закрытые статьи и
  сайты, требующие входа, не обходятся;
- размер файла, число страниц и объём текста ограничены;
- сканы без текстового слоя пропускаются (OCR в прототипе нет);
- сбой одной статьи не прерывает остальные.
"""
import io
from typing import Dict, List, Optional

import httpx
from pypdf import PdfReader
from sqlalchemy.orm import Session

from .. import document_processing, models
from ..sources.openalex.client import OpenAlexClient

SOURCE_LABEL = "полный текст (open access)"
MAX_BYTES = 20 * 1024 * 1024
MAX_PAGES = 60
MAX_CHARS = 150_000
MIN_TEXT_CHARS = 2_000


class FulltextError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def has_fulltext(pub: models.Publication) -> bool:
    return any((c.source_filename or "").startswith("полный текст") for c in pub.chunks)


async def find_pdf_url(client: OpenAlexClient, doi: str) -> Optional[str]:
    work = await client.get_work_by_doi(doi)
    if not work:
        return None
    best = work.get("best_oa_location") or {}
    url = best.get("pdf_url") or (work.get("primary_location") or {}).get("pdf_url")
    if url:
        return url
    for loc in work.get("locations") or []:
        if loc and loc.get("pdf_url"):
            return loc["pdf_url"]
    return None


async def download_pdf(url: str, http: httpx.AsyncClient) -> bytes:
    if not url.lower().startswith(("http://", "https://")):
        raise FulltextError("not_pdf", "неподдерживаемая схема ссылки")
    async with http.stream("GET", url, follow_redirects=True) as resp:
        resp.raise_for_status()
        declared = int(resp.headers.get("content-length") or 0)
        if declared > MAX_BYTES:
            raise FulltextError("too_big", f"{declared // 1024 // 1024} МБ")
        buf = bytearray()
        async for chunk in resp.aiter_bytes():
            buf += chunk
            if len(buf) > MAX_BYTES:
                raise FulltextError("too_big", f"более {MAX_BYTES // 1024 // 1024} МБ")
    if b"%PDF" not in bytes(buf[:1024]):
        raise FulltextError("not_pdf", "по ссылке не PDF (возможно, страница издателя)")
    return bytes(buf)


def extract_pdf_text(data: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            reader.decrypt("")
        parts: List[str] = []
        total = 0
        for page in reader.pages[:MAX_PAGES]:
            text = page.extract_text() or ""
            parts.append(text)
            total += len(text)
            if total > MAX_CHARS:
                break
        return document_processing.clean_text("\n".join(parts))[:MAX_CHARS]
    except Exception as e:
        raise FulltextError("no_text", f"{type(e).__name__}")


def store_fulltext(db: Session, pub: models.Publication, text: str) -> int:
    pieces = document_processing.chunk_text(text)
    for i, piece in enumerate(pieces, start=1):  # индекс 0 занят аннотацией
        db.add(models.DocumentChunk(
            publication_id=pub.publication_id, source_filename=SOURCE_LABEL, chunk_index=i, text=piece,
        ))
    db.flush()
    return len(pieces)


async def fetch_fulltexts(
    db: Session,
    publication_ids: Optional[List[int]] = None,
    limit: int = 15,
    oa_client: Optional[OpenAlexClient] = None,
    http: Optional[httpx.AsyncClient] = None,
) -> List[Dict]:
    """Скачивает и сохраняет полные тексты. Возвращает результат по каждой публикации."""
    from fastapi.concurrency import run_in_threadpool

    q = db.query(models.Publication)
    if publication_ids is not None:
        q = q.filter(models.Publication.publication_id.in_(publication_ids))
    else:
        q = q.filter(models.Publication.doi.isnot(None))
    pubs = [p for p in q.order_by(models.Publication.publication_id.desc()).all()]
    if publication_ids is None:
        pubs = [p for p in pubs if not has_fulltext(p)]
    pubs = pubs[: max(1, min(limit, 50))]

    oa = oa_client or OpenAlexClient()
    own_http = http is None
    http = http or httpx.AsyncClient(
        timeout=30.0, headers={"User-Agent": f"AcademicKB/1.0 (mailto:{oa.email or 'noreply@example.com'})"}
    )
    results: List[Dict] = []
    try:
        for pub in pubs:
            r = {"publication_id": pub.publication_id, "title": pub.title, "status": "error", "chunks": 0, "detail": ""}
            try:
                if has_fulltext(pub):
                    r["status"] = "already"
                elif not pub.doi:
                    r["status"] = "no_doi"
                else:
                    url = await find_pdf_url(oa, pub.doi)
                    if not url:
                        r["status"] = "no_open_access"
                    else:
                        data = await download_pdf(url, http)
                        text = await run_in_threadpool(extract_pdf_text, data)
                        if len(text) < MIN_TEXT_CHARS:
                            raise FulltextError("no_text", "в PDF нет текстового слоя (скан) или он слишком короткий")
                        r["chunks"] = store_fulltext(db, pub, text)
                        db.commit()
                        r["status"] = "ok"
            except FulltextError as e:
                db.rollback()
                r["status"], r["detail"] = e.code, e.detail
            except httpx.HTTPError as e:
                db.rollback()
                r["status"], r["detail"] = "download_failed", f"{type(e).__name__}"
            except Exception as e:  # одна статья не должна ронять остальные
                db.rollback()
                r["status"], r["detail"] = "error", f"{type(e).__name__}"
            results.append(r)
    finally:
        if own_http:
            await http.aclose()
    return results
