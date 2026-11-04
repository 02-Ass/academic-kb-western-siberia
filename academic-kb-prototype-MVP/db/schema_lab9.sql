-- =====================================================================
-- Лабораторная работа №9
-- Физическая модель данных. Диаграммы потоков данных ERD (IDEF1X)
-- Тема: Академическая база знаний по исследованиям и разработке
--       нефтегазовых месторождений Западной Сибири
--
-- СУБД: PostgreSQL
-- Скрипт создаёт схему ровно по физической модели из раздела 3 работы:
-- province → oblast → rayon → field → horizon → deposit,
-- плюс publication, author, research_type и три ассоциативные
-- сущности (publication_author, publication_research_type,
-- publication_field).
--
-- Запуск:
--   psql -U postgres -d <ваша_база> -f schema_lab9.sql
-- =====================================================================

-- Раздел 2 работы: pgvector обосновывает выбор PostgreSQL для хранения
-- LLM-эмбеддингов в будущем. Сами эмбеддинги НЕ входят в перечень
-- атрибутов сущностей из раздела 3 — ни одна таблица ниже не содержит
-- колонку embedding. Поэтому подключение расширения закомментировано:
-- pgvector нужно ставить отдельно (как PostGIS), и этот скрипт не должен
-- падать на машине, где он ещё не установлен. Если он у вас уже стоит
-- (или встроен в выбранный Docker-образ Postgres) — раскомментируйте:
-- CREATE EXTENSION IF NOT EXISTS vector;

BEGIN;

-- ---------------------------------------------------------------------
-- 3.1. province (Провинция)
-- ---------------------------------------------------------------------
CREATE TABLE province (
    province_id   SERIAL PRIMARY KEY,
    name          VARCHAR(100) NOT NULL,
    description   TEXT
);

COMMENT ON TABLE  province               IS 'Нефтегазоносная провинция — верхний уровень иерархии';
COMMENT ON COLUMN province.province_id   IS 'Первичный ключ';
COMMENT ON COLUMN province.name          IS 'Наименование провинции';
COMMENT ON COLUMN province.description   IS 'Описание';

-- ---------------------------------------------------------------------
-- 3.2. oblast (Область)
-- ---------------------------------------------------------------------
CREATE TABLE oblast (
    oblast_id     SERIAL PRIMARY KEY,
    province_id   INT NOT NULL REFERENCES province (province_id),
    name          VARCHAR(100) NOT NULL,
    description   TEXT
);

COMMENT ON TABLE  oblast               IS 'Нефтегазоносная область';
COMMENT ON COLUMN oblast.oblast_id     IS 'Первичный ключ';
COMMENT ON COLUMN oblast.province_id   IS 'Внешний ключ к сущности province';
COMMENT ON COLUMN oblast.name          IS 'Наименование области';
COMMENT ON COLUMN oblast.description   IS 'Описание';

CREATE INDEX idx_oblast_province_id ON oblast (province_id);

-- ---------------------------------------------------------------------
-- 3.3. rayon (Район)
-- ---------------------------------------------------------------------
CREATE TABLE rayon (
    rayon_id      SERIAL PRIMARY KEY,
    oblast_id     INT NOT NULL REFERENCES oblast (oblast_id),
    name          VARCHAR(100) NOT NULL,
    description   TEXT
);

COMMENT ON TABLE  rayon              IS 'Нефтеносный район';
COMMENT ON COLUMN rayon.rayon_id     IS 'Первичный ключ';
COMMENT ON COLUMN rayon.oblast_id    IS 'Внешний ключ к сущности oblast';
COMMENT ON COLUMN rayon.name         IS 'Наименование района';
COMMENT ON COLUMN rayon.description  IS 'Описание';

CREATE INDEX idx_rayon_oblast_id ON rayon (oblast_id);

-- ---------------------------------------------------------------------
-- 3.4. field (Месторождение)
-- ---------------------------------------------------------------------
CREATE TABLE field (
    field_id         SERIAL PRIMARY KEY,
    rayon_id         INT NOT NULL REFERENCES rayon (rayon_id),
    name             VARCHAR(100) NOT NULL,
    discovery_date   DATE,
    coordinates      VARCHAR(50),
    depth            NUMERIC(10, 2),
    water_cut        NUMERIC(5, 2),
    description      TEXT
);

COMMENT ON TABLE  field                   IS 'Месторождение, например «Шаимское»';
COMMENT ON COLUMN field.field_id          IS 'Первичный ключ';
COMMENT ON COLUMN field.rayon_id          IS 'Внешний ключ к сущности rayon';
COMMENT ON COLUMN field.name              IS 'Наименование месторождения, например «Шаимское»';
COMMENT ON COLUMN field.discovery_date    IS 'Дата открытия месторождения';
COMMENT ON COLUMN field.coordinates       IS 'Географические координаты (широта и долгота)';
COMMENT ON COLUMN field.depth             IS 'Средняя глубина залегания, м';
COMMENT ON COLUMN field.water_cut         IS 'Обводнённость, %';
COMMENT ON COLUMN field.description       IS 'Дополнительная информация';

CREATE INDEX idx_field_rayon_id ON field (rayon_id);

-- ---------------------------------------------------------------------
-- 3.5. horizon (Пласт)
-- ---------------------------------------------------------------------
CREATE TABLE horizon (
    horizon_id       SERIAL PRIMARY KEY,
    field_id         INT NOT NULL REFERENCES field (field_id),
    index_code       VARCHAR(20) NOT NULL,
    formation        VARCHAR(50),
    thickness        NUMERIC(10, 2),
    reservoir_type   VARCHAR(50),
    permeability     NUMERIC(10, 2),
    porosity         NUMERIC(5, 2)
);

COMMENT ON TABLE  horizon                  IS 'Продуктивный пласт';
COMMENT ON COLUMN horizon.horizon_id       IS 'Первичный ключ';
COMMENT ON COLUMN horizon.field_id         IS 'Внешний ключ к сущности field';
COMMENT ON COLUMN horizon.index_code       IS 'Индекс пласта, например «Т1», «Т2», «Т3»';
COMMENT ON COLUMN horizon.formation        IS 'Свита, например «тюменская»';
COMMENT ON COLUMN horizon.thickness        IS 'Мощность пласта, м';
COMMENT ON COLUMN horizon.reservoir_type   IS 'Тип коллектора (тип породы)';
COMMENT ON COLUMN horizon.permeability     IS 'Проницаемость, мД';
COMMENT ON COLUMN horizon.porosity         IS 'Пористость, %';

CREATE INDEX idx_horizon_field_id ON horizon (field_id);

-- ---------------------------------------------------------------------
-- 3.6. deposit (Залежь)
-- ---------------------------------------------------------------------
CREATE TABLE deposit (
    deposit_id    SERIAL PRIMARY KEY,
    horizon_id    INT NOT NULL REFERENCES horizon (horizon_id),
    name          VARCHAR(100) NOT NULL,
    oil_type      VARCHAR(50),
    reserves      NUMERIC(15, 2)
);

COMMENT ON TABLE  deposit               IS 'Залежь углеводородов';
COMMENT ON COLUMN deposit.deposit_id    IS 'Первичный ключ';
COMMENT ON COLUMN deposit.horizon_id    IS 'Внешний ключ к сущности horizon';
COMMENT ON COLUMN deposit.name          IS 'Наименование залежи';
COMMENT ON COLUMN deposit.oil_type      IS 'Тип нефти';
COMMENT ON COLUMN deposit.reserves      IS 'Запасы';

CREATE INDEX idx_deposit_horizon_id ON deposit (horizon_id);

-- ---------------------------------------------------------------------
-- 3.9. research_type (Тип исследования)
-- ---------------------------------------------------------------------
CREATE TABLE research_type (
    type_id       SERIAL PRIMARY KEY,
    name          VARCHAR(100) NOT NULL,
    description   TEXT
);

COMMENT ON TABLE  research_type               IS 'Тип (вид) научного исследования';
COMMENT ON COLUMN research_type.type_id       IS 'Первичный ключ';
COMMENT ON COLUMN research_type.name          IS 'Наименование типа исследования';
COMMENT ON COLUMN research_type.description   IS 'Описание';

-- ---------------------------------------------------------------------
-- 3.8. author (Автор)
-- ---------------------------------------------------------------------
CREATE TABLE author (
    author_id      SERIAL PRIMARY KEY,
    last_name      VARCHAR(50) NOT NULL,
    first_name     VARCHAR(50) NOT NULL,
    middle_name    VARCHAR(50),
    affiliation    VARCHAR(200),
    email          VARCHAR(100)
);

COMMENT ON TABLE  author                IS 'Автор научной публикации';
COMMENT ON COLUMN author.author_id      IS 'Первичный ключ';
COMMENT ON COLUMN author.last_name      IS 'Фамилия автора';
COMMENT ON COLUMN author.first_name     IS 'Имя автора';
COMMENT ON COLUMN author.middle_name    IS 'Отчество автора';
COMMENT ON COLUMN author.affiliation    IS 'Место работы/учёбы';
COMMENT ON COLUMN author.email          IS 'Контактный email';

-- ---------------------------------------------------------------------
-- 3.7. publication (Публикация)
-- ---------------------------------------------------------------------
CREATE TABLE publication (
    publication_id   SERIAL PRIMARY KEY,
    title            VARCHAR(500) NOT NULL,
    year             INT,
    journal          VARCHAR(200),
    doi              VARCHAR(100),
    type             VARCHAR(50) NOT NULL,
    link             VARCHAR(500),
    file_path        TEXT
);

COMMENT ON TABLE  publication                  IS 'Научная публикация (статья, отчёт, карта и т.п.)';
COMMENT ON COLUMN publication.publication_id   IS 'Первичный ключ';
COMMENT ON COLUMN publication.title            IS 'Полное название публикации';
COMMENT ON COLUMN publication.year             IS 'Год публикации';
COMMENT ON COLUMN publication.journal          IS 'Название журнала или сборника';
COMMENT ON COLUMN publication.doi              IS 'Цифровой идентификатор DOI';
COMMENT ON COLUMN publication.type             IS 'Тип публикации: статья, отчёт, карта';
COMMENT ON COLUMN publication.link             IS 'URL источника';
COMMENT ON COLUMN publication.file_path        IS 'Путь к файлу документа в хранилище';

-- ---------------------------------------------------------------------
-- 3.10. publication_author — ассоциативная сущность (связь M:N)
-- ---------------------------------------------------------------------
CREATE TABLE publication_author (
    link_id          SERIAL PRIMARY KEY,
    publication_id   INT NOT NULL REFERENCES publication (publication_id),
    author_id        INT NOT NULL REFERENCES author (author_id),
    author_role      VARCHAR(50),
    author_order     INT
);

COMMENT ON TABLE  publication_author                  IS 'Связь публикации с авторами (многие-ко-многим)';
COMMENT ON COLUMN publication_author.link_id           IS 'Первичный ключ';
COMMENT ON COLUMN publication_author.publication_id    IS 'Внешний ключ к публикации';
COMMENT ON COLUMN publication_author.author_id         IS 'Внешний ключ к автору';
COMMENT ON COLUMN publication_author.author_role       IS 'Роль автора, например «научный руководитель»';
COMMENT ON COLUMN publication_author.author_order      IS 'Порядок автора в списке (1 — первый автор)';

CREATE INDEX idx_publication_author_publication_id ON publication_author (publication_id);
CREATE INDEX idx_publication_author_author_id      ON publication_author (author_id);
-- Одна и та же пара публикация/автор не должна дублироваться
ALTER TABLE publication_author
    ADD CONSTRAINT uq_publication_author UNIQUE (publication_id, author_id);

-- ---------------------------------------------------------------------
-- 3.11. publication_research_type — ассоциативная сущность
-- ---------------------------------------------------------------------
CREATE TABLE publication_research_type (
    link_id          SERIAL PRIMARY KEY,
    publication_id   INT NOT NULL REFERENCES publication (publication_id),
    type_id          INT NOT NULL REFERENCES research_type (type_id)
);

COMMENT ON TABLE  publication_research_type                IS 'Связь публикации с типами исследования (многие-ко-многим)';
COMMENT ON COLUMN publication_research_type.link_id         IS 'Первичный ключ';
COMMENT ON COLUMN publication_research_type.publication_id  IS 'Внешний ключ к публикации';
COMMENT ON COLUMN publication_research_type.type_id         IS 'Внешний ключ к типу исследования';

CREATE INDEX idx_pub_research_type_publication_id ON publication_research_type (publication_id);
CREATE INDEX idx_pub_research_type_type_id        ON publication_research_type (type_id);
ALTER TABLE publication_research_type
    ADD CONSTRAINT uq_publication_research_type UNIQUE (publication_id, type_id);

-- ---------------------------------------------------------------------
-- 3.12. publication_field — связь публикации с нефтегазовыми объектами
-- ---------------------------------------------------------------------
CREATE TABLE publication_field (
    link_id           SERIAL PRIMARY KEY,
    publication_id    INT NOT NULL REFERENCES publication (publication_id),
    field_id          INT NOT NULL REFERENCES field (field_id),
    relevance_score   NUMERIC(5, 2)
);

COMMENT ON TABLE  publication_field                  IS 'Связь публикации с месторождением (многие-ко-многим)';
COMMENT ON COLUMN publication_field.link_id           IS 'Первичный ключ';
COMMENT ON COLUMN publication_field.publication_id    IS 'Внешний ключ к публикации';
COMMENT ON COLUMN publication_field.field_id          IS 'Внешний ключ к месторождению';
COMMENT ON COLUMN publication_field.relevance_score   IS 'Степень релевантности публикации объекту';

CREATE INDEX idx_publication_field_publication_id ON publication_field (publication_id);
CREATE INDEX idx_publication_field_field_id       ON publication_field (field_id);
ALTER TABLE publication_field
    ADD CONSTRAINT uq_publication_field UNIQUE (publication_id, field_id);

-- ---------------------------------------------------------------------
-- publication_horizon — связь публикации с пластом (по аналогии с
-- publication_field; не входит в исходный раздел 3 лабораторной
-- работы, добавлено по вашему запросу, чтобы публикацию можно было
-- привязать напрямую к конкретному пласту, а не только к месторождению)
-- ---------------------------------------------------------------------
CREATE TABLE publication_horizon (
    link_id           SERIAL PRIMARY KEY,
    publication_id    INT NOT NULL REFERENCES publication (publication_id),
    horizon_id        INT NOT NULL REFERENCES horizon (horizon_id),
    relevance_score   NUMERIC(5, 2)
);

COMMENT ON TABLE  publication_horizon                  IS 'Связь публикации с пластом (многие-ко-многим)';
COMMENT ON COLUMN publication_horizon.link_id           IS 'Первичный ключ';
COMMENT ON COLUMN publication_horizon.publication_id    IS 'Внешний ключ к публикации';
COMMENT ON COLUMN publication_horizon.horizon_id        IS 'Внешний ключ к пласту';
COMMENT ON COLUMN publication_horizon.relevance_score   IS 'Степень релевантности публикации пласту';

CREATE INDEX idx_publication_horizon_publication_id ON publication_horizon (publication_id);
CREATE INDEX idx_publication_horizon_horizon_id     ON publication_horizon (horizon_id);
ALTER TABLE publication_horizon
    ADD CONSTRAINT uq_publication_horizon UNIQUE (publication_id, horizon_id);

-- ---------------------------------------------------------------------
-- publication_deposit — связь публикации с залежью (по аналогии с
-- publication_field; тоже добавлено по вашему запросу)
-- ---------------------------------------------------------------------
CREATE TABLE publication_deposit (
    link_id           SERIAL PRIMARY KEY,
    publication_id    INT NOT NULL REFERENCES publication (publication_id),
    deposit_id        INT NOT NULL REFERENCES deposit (deposit_id),
    relevance_score   NUMERIC(5, 2)
);

COMMENT ON TABLE  publication_deposit                  IS 'Связь публикации с залежью (многие-ко-многим)';
COMMENT ON COLUMN publication_deposit.link_id           IS 'Первичный ключ';
COMMENT ON COLUMN publication_deposit.publication_id    IS 'Внешний ключ к публикации';
COMMENT ON COLUMN publication_deposit.deposit_id        IS 'Внешний ключ к залежи';
COMMENT ON COLUMN publication_deposit.relevance_score   IS 'Степень релевантности публикации залежи';

CREATE INDEX idx_publication_deposit_publication_id ON publication_deposit (publication_id);
CREATE INDEX idx_publication_deposit_deposit_id     ON publication_deposit (deposit_id);
ALTER TABLE publication_deposit
    ADD CONSTRAINT uq_publication_deposit UNIQUE (publication_id, deposit_id);

COMMIT;
