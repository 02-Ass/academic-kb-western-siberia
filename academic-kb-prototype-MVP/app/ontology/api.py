"""REST API онтологии: сводка, объекты, запросы с выводом и объяснением, SPARQL, согласованность, экспорт."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from . import mapping, queries, reasoner

router = APIRouter(prefix="/api/ontology", tags=["ontology"])


class SparqlRequest(BaseModel):
    query: str
    inferred: bool = True  # True — по графу с выведенными фактами, False — только записанное в базе


@router.get("/summary")
def summary(db: Session = Depends(get_db)):
    """Размер онтологии, число утверждённых и выведенных фактов, согласованность, примеры вывода."""
    return queries.summary(reasoner.get_state(db))


@router.get("/mapping")
def mapping_table(db: Session = Depends(get_db)):
    """Соответствие онтологии (ваш файл) и БД: какой класс/свойство какой таблице, столбцу или ключу отвечает."""
    st = reasoner.get_state(db)
    return {"base": {"source": st.base.source, "path": st.base.path}, "rows": mapping.mapping_rows(st),
            "extensions": st.extensions}


@router.get("/objects")
def objects(db: Session = Depends(get_db)):
    st = reasoner.get_state(db)
    return {"objects": queries.list_objects(st),
            "types": [{"id": i, "name": t["name"]} for i, t in sorted(st.reg.types.items())]}


@router.get("/publications")
def publications(
    kind: str = Query(..., description="province | oblast | rayon | field | horizon | deposit"),
    id: int = Query(...),
    type_id: Optional[int] = None,
    inference: bool = True,
    db: Session = Depends(get_db),
):
    """Публикации по объекту (и типу исследования) с учётом или без учёта логического вывода;
    для каждой публикации — цепочка рассуждения."""
    try:
        return queries.publications_for(reasoner.get_state(db), kind, id, type_id, inference)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))


@router.post("/sparql")
def run_sparql(payload: SparqlRequest, db: Session = Depends(get_db)):
    """SPARQL SELECT/ASK. Префиксы (: rdfs: rdf: owl: xsd:) подставляются сами."""
    try:
        return queries.sparql(reasoner.get_state(db), payload.query, payload.inferred)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/check")
def check(demo: bool = False, db: Session = Depends(get_db)):
    """Проверка согласованности. demo=true добавляет в КОПИЮ онтологии намеренную ошибку и показывает,
    как рассуждатель её находит."""
    return queries.check(reasoner.get_state(db), demo)


@router.get("/validate")
def validate(db: Session = Depends(get_db)):
    """Сверка вывода с рекурсивными запросами к реляционной базе: должны совпасть для каждого объекта."""
    return queries.validate_against_sql(reasoner.get_state(db), db)


@router.get("/export")
def export(format: str = Query("owl", pattern="^(owl|ttl)$"), inferred: bool = False, db: Session = Depends(get_db)):
    """Файл онтологии: owl (RDF/XML) открывается в Protégé, ttl — Turtle. inferred=true добавляет выведенные факты."""
    body, mime, name = queries.export(reasoner.get_state(db), format, inferred)
    return Response(content=body, media_type=mime, headers={"Content-Disposition": f'attachment; filename="{name}"'})
