// Минимальный и безопасный рендер Markdown для ответов LLM: заголовки,
// списки, таблицы (GFM), **жирный**, *курсив*, кликабельные ссылки. Без внешних
// зависимостей и без dangerouslySetInnerHTML — весь текст остаётся текстом.
const URL_RE = /(https?:\/\/[^\s)<>]+[^\s).,;<>])/g;

function linkify(text, keyPrefix) {
  return text.split(URL_RE).map((part, i) =>
    /^https?:\/\//.test(part) ? (
      <a key={`${keyPrefix}-l${i}`} href={part} target="_blank" rel="noreferrer">
        {part}
      </a>
    ) : (
      part
    )
  );
}

// [3], [1, 4] — номера источников; [?] — ссылка, которую проверка удалила
const CITE_RE = /(\[\d+(?:\s*,\s*\d+)*\]|\[\?\])/g;

function renderPlain(text, keyPrefix, sources) {
  return text.split(CITE_RE).map((part, i) => {
    const key = `${keyPrefix}-c${i}`;
    if (part === "[?]") {
      return (
        <span key={key} className="cite-bad" title="Ссылка на несуществующий источник удалена проверкой">[?]</span>
      );
    }
    if (/^\[\d/.test(part)) {
      const nums = part.slice(1, -1).split(",").map((x) => Number(x.trim()));
      return (
        <span key={key} className="cite">
          [
          {nums.map((n, j) => {
            const src = sources.find((x) => x.n === n);
            return (
              <span key={j}>
                {j > 0 && ", "}
                {src?.url ? (
                  <a href={src.url} target="_blank" rel="noreferrer" title={src.title}>{n}</a>
                ) : (
                  <span title={src?.title}>{n}</span>
                )}
              </span>
            );
          })}
          ]
        </span>
      );
    }
    return <span key={key}>{linkify(part, key)}</span>;
  });
}

function renderInline(text, keyPrefix, sources = []) {
  const parts = text.split(/(\*\*[^*]+\*\*|\*[^*\s][^*]*\*)/g);
  return parts.map((part, i) => {
    const key = `${keyPrefix}-${i}`;
    if (part.startsWith("**") && part.endsWith("**") && part.length > 4) {
      return <strong key={key}>{renderPlain(part.slice(2, -2), key, sources)}</strong>;
    }
    if (part.startsWith("*") && part.endsWith("*") && part.length > 2) {
      return <em key={key}>{renderPlain(part.slice(1, -1), key, sources)}</em>;
    }
    return <span key={key}>{renderPlain(part, key, sources)}</span>;
  });
}

// --- таблицы GFM: | Параметр | A | B | с разделителем | :--- | --- | ---: | ---
const TABLE_SEP = /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$/;

function splitRow(line) {
  let t = line.trim();
  if (t.startsWith("|")) t = t.slice(1);
  if (t.endsWith("|") && !t.endsWith("\\|")) t = t.slice(0, -1);
  return t.split(/(?<!\\)\|/).map((c) => c.replace(/\\\|/g, "|").trim());
}

function alignOf(cell) {
  const left = cell.startsWith(":"), right = cell.endsWith(":");
  return left && right ? "center" : right ? "right" : "left";
}

export default function Markdown({ text, sources = [] }) {
  const blocks = [];
  let list = null; // {type: "ul" | "ol", items: []}

  const flushList = () => {
    if (list) {
      blocks.push(list);
      list = null;
    }
  };

  const lines = (text || "").split("\n");
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trimEnd();
    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    const bullet = line.match(/^\s*[-*•]\s+(.*)$/);
    const numbered = line.match(/^\s*(\d+)[.)]\s+(.*)$/);

    // таблица: строка с «|» и сразу за ней строка-разделитель
    if (line.includes("|") && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1]) && lines[i + 1].includes("-")) {
      flushList();
      const head = splitRow(line);
      const aligns = splitRow(lines[i + 1]).map(alignOf);
      const rows = [];
      i += 2;
      while (i < lines.length && lines[i].trim() !== "" && lines[i].includes("|")) {
        rows.push(splitRow(lines[i]));
        i++;
      }
      i--; // внешний цикл сам перейдёт к следующей строке
      blocks.push({ type: "table", head, aligns, rows });
      continue;
    }

    if (heading) {
      flushList();
      blocks.push({ type: "h", text: heading[2] });
    } else if (bullet) {
      if (!list || list.type !== "ul") {
        flushList();
        list = { type: "ul", items: [] };
      }
      list.items.push(bullet[1]);
    } else if (numbered) {
      if (!list || list.type !== "ol") {
        flushList();
        list = { type: "ol", items: [] };
      }
      list.items.push(numbered[2]);
    } else if (line.trim() === "") {
      flushList();
    } else {
      flushList();
      blocks.push({ type: "p", text: line });
    }
  }
  flushList();

  return (
    <div className="md">
      {blocks.map((b, i) => {
        if (b.type === "h") return <h4 key={i}>{renderInline(b.text, `h${i}`, sources)}</h4>;
        if (b.type === "table") {
          const cols = b.head.length;
          return (
            <div className="md-table-wrap" key={i}>
              <table>
                <thead>
                  <tr>
                    {b.head.map((c, j) => (
                      <th key={j} style={{ textAlign: b.aligns[j] || "left" }}>{renderInline(c, `th${i}-${j}`, sources)}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {b.rows.map((row, r) => (
                    <tr key={r}>
                      {Array.from({ length: cols }, (_, j) => (
                        <td key={j} style={{ textAlign: b.aligns[j] || "left" }}>{renderInline(row[j] || "", `td${i}-${r}-${j}`, sources)}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          );
        }
        if (b.type === "p") return <p key={i}>{renderInline(b.text, `p${i}`, sources)}</p>;
        const Tag = b.type;
        return (
          <Tag key={i}>
            {b.items.map((it, j) => (
              <li key={j}>{renderInline(it, `l${i}-${j}`, sources)}</li>
            ))}
          </Tag>
        );
      })}
    </div>
  );
}
