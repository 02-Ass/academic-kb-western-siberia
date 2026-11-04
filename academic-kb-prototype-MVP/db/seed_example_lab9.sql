-- =====================================================================
-- Пример наполнения схемы (schema_lab9.sql) данными по объекту
-- апробации работы — Шаимский нефтеносный район.
-- Выполнять ПОСЛЕ schema_lab9.sql.
--
-- Запуск:
--   psql -U postgres -d <ваша_база> -f seed_example_lab9.sql
-- =====================================================================

BEGIN;

INSERT INTO province (name, description) VALUES
    ('Западно-Сибирская нефтегазоносная провинция', 'Крупнейшая нефтегазоносная провинция России');

INSERT INTO oblast (province_id, name, description) VALUES
    (1, 'Приуральская нефтегазоносная область', 'Территория концентрации месторождений Шаимского района');

INSERT INTO rayon (oblast_id, name, description) VALUES
    (1, 'Шаимский нефтеносный район', 'Объект апробации разрабатываемой системы');

-- Значения в полях месторождений взяты из источников; всё, что подтвердить не удалось,
-- оставлено пустым (NULL), а не выдумано. Источники:
--   Шаимское   — ЦДУ ТЭК (cdu.ru), 2020: открыто 21.06.1960, разработка с 1964 г.; координаты не найдены
--   Шушминское — каталог НефтьГазИнформ (oilgasinform.ru): открыто в 1985 г., координаты 61.166667, 64.133333
--   Ловинское  — nftn.ru: открыто в 1973 г., координаты 61.25, 65.00
-- Для месторождений, где известен только год, в поле DATE условно указано 1 января.
INSERT INTO field (rayon_id, name, discovery_date, coordinates, depth, water_cut, description) VALUES
    (1, 'Шаимское',   '1960-06-21', NULL,                  NULL, NULL, 'Первое промышленное месторождение нефти Западной Сибири (Трёхозёрное), скважина Р-6'),
    (1, 'Шушминское', '1985-01-01', '61.166667,64.133333', NULL, NULL, 'Открыто в 1985 г.'),
    (1, 'Ловинское',  '1973-01-01', '61.25,65.00',         NULL, NULL, 'Открыто в 1973 г.');

INSERT INTO horizon (field_id, index_code, formation, thickness, reservoir_type, permeability, porosity) VALUES
    (1, 'Т1', 'тюменская', NULL, NULL, NULL, NULL);

INSERT INTO deposit (horizon_id, name, oil_type, reserves) VALUES
    (1, 'Шаимская залежь', 'лёгкая', NULL);

INSERT INTO research_type (name, description) VALUES
    ('Геологическое',       'Исследования геологического строения'),
    ('Геофизическое',       'Каротаж, сейсморазведка'),
    ('Гидрогеологическое',  'Изучение подземных вод'),
    ('Геодинамическое',     'Анализ тектонических процессов');

INSERT INTO author (last_name, first_name, middle_name, affiliation) VALUES
    ('Иванов',   'Иван',    'Иванович', 'Тюменский индустриальный университет'),
    ('Петров',   'Алексей', 'Алексеевич', NULL),
    ('Сидоров',  'Андрей',  'Андреевич', NULL);

INSERT INTO publication (title, year, journal, type, link) VALUES
    ('Геологическое строение Шаимского района',           2005, 'Геология нефти и газа', 'статья', 'https://example.ru'),
    ('Геофизические исследования Шаимского месторождения', 2010, 'Нефтяное хозяйство',    'статья', 'https://example.ru/sidorov2010');

INSERT INTO publication_author (publication_id, author_id, author_role, author_order) VALUES
    (1, 1, 'автор', 1),
    (2, 3, 'автор', 1);

INSERT INTO publication_research_type (publication_id, type_id) VALUES
    (1, 1),  -- Геологическое строение Шаимского района → Геологическое
    (2, 1);  -- Геофизические исследования → Геологическое (геофизика как подраздел)

INSERT INTO publication_field (publication_id, field_id, relevance_score) VALUES
    (1, 1, 1.00),
    (2, 1, 0.95);

COMMIT;
