"""
Генерация онтологии с помощью LLM по методам из статьи Lippolis et al., «Ontology Generation using Large
Language Models» (arXiv 2503.05388):

  cqbycq    — Memoryless CQbyCQ: на каждый компетентностный вопрос (CQ) отдельный запрос БЕЗ памяти; LLM
              возвращает фрагмент онтологии (Turtle), фрагменты потом объединяются.
  ontogenia — Ontogenia: один диалог с памятью, CQ подаются по одному, LLM каждый раз получает текущую
              онтологию и расширяет её (метакогнитивная подсказка: проверить, нужны ли новые элементы).

Запуск (нужен ключ в .env: DEEPSEEK_API_KEY, как у чата):
    python -m scripts.generate_ontology_llm --method cqbycq    --out ontology_llm/generated/deepseek_cqbycq
    python -m scripts.generate_ontology_llm --method ontogenia --out ontology_llm/generated/deepseek_ontogenia
Затем:
    python -m scripts.compare_ontologies --extra deepseek_cqbycq=ontology_llm/generated/deepseek_cqbycq/merged.ttl
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:                                    # .env, как в приложении
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass

from rdflib import Graph

from app import llm

SYSTEM = ("You are an ontology engineer. You build OWL 2 ontologies in Turtle. "
          "Always return ONLY valid Turtle (no explanations, no markdown fences).")

PREFIXES = ("@prefix : <http://example.org/westsib#> .\n@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
            "@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .\n@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
            "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .\n")

CQBYCQ_PROMPT = """Persona: you are an expert ontology engineer for the petroleum geology domain.
Task: model the ontology fragment needed to answer the competency question below.
Context (user story): {story}
Competency question: {cq}
Rules:
- Use the prefix `:` = <http://example.org/westsib#>; declare every class and property you use (owl:Class, owl:ObjectProperty, owl:DatatypeProperty).
- Add rdfs:domain, rdfs:range, rdfs:label (en and ru), rdfs:comment; add inverse/transitive/subclass axioms only when they are justified.
- Reuse well-known ontology design patterns where suitable (e.g. part-of, agent/role, time-indexed information).
- Output ONLY the Turtle for this competency question, starting with the @prefix lines."""

ONTOGENIA_PROMPT = """Persona: you are an expert ontology engineer for the petroleum geology domain.
Context (user story): {story}
Current ontology (Turtle):
{current}
New competency question: {cq}
Think step by step (metacognitive check): (1) can the current ontology already answer this question? (2) if not, which minimal new classes, properties and axioms are needed? (3) do they duplicate or contradict existing elements? (4) are domains, ranges and inverses correct?
Return the COMPLETE updated ontology in Turtle (all previous content kept, new elements added), starting with the @prefix lines. Use prefix `:` = <http://example.org/westsib#>."""


def ask(client, model, prompt, retries=3):
    for i in range(retries):
        try:
            r = client.chat.completions.create(model=model, temperature=0,
                                               messages=[{"role": "system", "content": SYSTEM},
                                                         {"role": "user", "content": prompt}])
            return r.choices[0].message.content or ""
        except Exception as e:                       # сеть/лимиты
            print("  ошибка запроса:", e)
            time.sleep(3 * (i + 1))
    return ""


def clean(text: str) -> str:
    text = re.sub(r"^```[a-zA-Z]*\s*|```\s*$", "", text.strip(), flags=re.M).strip()
    return text if "@prefix" in text else PREFIXES + text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--method", choices=["cqbycq", "ontogenia"], default="cqbycq")
    ap.add_argument("--out", required=True)
    ap.add_argument("--cq", default=str(ROOT / "ontology_llm" / "cq.json"))
    a = ap.parse_args()

    client, prov = llm._get_client(), llm._active_provider()
    if client is None:
        sys.exit("Не задан ключ LLM (DEEPSEEK_API_KEY в .env).")
    data = json.loads(Path(a.cq).read_text(encoding="utf-8"))
    story = data["user_story"]["en"]
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "provider.txt").write_text(f"{prov['label']} / {prov['model']} / method={a.method}\n", encoding="utf-8")
    print("LLM:", prov["label"], prov["model"], "| метод:", a.method)

    merged = Graph()
    current = PREFIXES
    for cq in data["cqs"]:
        print(cq["id"], cq["en"])
        if a.method == "cqbycq":
            ttl = clean(ask(client, prov["model"], CQBYCQ_PROMPT.format(story=story, cq=cq["en"])))
        else:
            ttl = clean(ask(client, prov["model"], ONTOGENIA_PROMPT.format(story=story, current=current, cq=cq["en"])))
        (out / f"{cq['id']}.ttl").write_text(ttl, encoding="utf-8")
        try:
            g = Graph()
            g.parse(data=ttl, format="turtle")
        except Exception as e:
            print("  фрагмент не разобран, пропущен:", str(e)[:120])
            continue
        if a.method == "cqbycq":
            merged += g
        else:
            merged, current = g, ttl
    merged.serialize(out / "merged.ttl", format="turtle")
    print("Готово:", out / "merged.ttl", "| триплетов:", len(merged))


if __name__ == "__main__":
    main()
