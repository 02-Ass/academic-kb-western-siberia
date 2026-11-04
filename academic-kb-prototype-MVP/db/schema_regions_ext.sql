-- Расширение схемы лаб. работы №9: привязка публикаций к объектам верхних уровней иерархии.
-- Приложение создаёт эти таблицы само при запуске (SQLAlchemy create_all); файл нужен для документации
-- и ручного развёртывания. Данные существующих таблиц не затрагиваются.

CREATE TABLE IF NOT EXISTS publication_rayon (
    link_id         SERIAL PRIMARY KEY,
    publication_id  INTEGER NOT NULL REFERENCES publication (publication_id),
    rayon_id        INTEGER NOT NULL REFERENCES rayon (rayon_id),
    relevance_score NUMERIC(5, 2)
);
CREATE TABLE IF NOT EXISTS publication_oblast (
    link_id         SERIAL PRIMARY KEY,
    publication_id  INTEGER NOT NULL REFERENCES publication (publication_id),
    oblast_id       INTEGER NOT NULL REFERENCES oblast (oblast_id),
    relevance_score NUMERIC(5, 2)
);
CREATE TABLE IF NOT EXISTS publication_province (
    link_id         SERIAL PRIMARY KEY,
    publication_id  INTEGER NOT NULL REFERENCES publication (publication_id),
    province_id     INTEGER NOT NULL REFERENCES province (province_id),
    relevance_score NUMERIC(5, 2)
);
COMMENT ON TABLE publication_rayon    IS 'Публикация о нефтеносном районе в целом';
COMMENT ON TABLE publication_oblast   IS 'Публикация о нефтегазоносной области в целом';
COMMENT ON TABLE publication_province IS 'Публикация о нефтегазоносной провинции в целом';
CREATE INDEX IF NOT EXISTS idx_publication_rayon_publication_id    ON publication_rayon (publication_id);
CREATE INDEX IF NOT EXISTS idx_publication_rayon_rayon_id          ON publication_rayon (rayon_id);
CREATE INDEX IF NOT EXISTS idx_publication_oblast_publication_id   ON publication_oblast (publication_id);
CREATE INDEX IF NOT EXISTS idx_publication_oblast_oblast_id        ON publication_oblast (oblast_id);
CREATE INDEX IF NOT EXISTS idx_publication_province_publication_id ON publication_province (publication_id);
CREATE INDEX IF NOT EXISTS idx_publication_province_province_id    ON publication_province (province_id);
