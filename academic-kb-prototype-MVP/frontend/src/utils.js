// Ссылка на публикацию: DOI приоритетнее — это постоянная ссылка, которая
// ведёт на страницу издателя; если DOI нет, используется адрес страницы.
export function doiUrl(doi) {
  if (!doi) return null;
  return doi.startsWith("http") ? doi : `https://doi.org/${doi}`;
}

export function publicationHref(p) {
  return doiUrl(p.doi) || p.link || p.url || null;
}
