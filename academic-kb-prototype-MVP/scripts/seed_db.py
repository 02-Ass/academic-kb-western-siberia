"""
Наполнение базы данных тестовыми данными по новой схеме (Лабораторная
работа №9, db/schema_lab9.sql) — объект апробации: Шаимский нефтеносный
район.

Запуск:
    python -m scripts.seed_db
"""
import sys
from pathlib import Path
from datetime import date

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import Base, engine, SessionLocal
from app import models


def run():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    try:
        province = models.Province(
            name="Западно-Сибирская нефтегазоносная провинция",
            description="Крупнейшая нефтегазоносная провинция России.",
        )
        db.add(province); db.flush()

        oblast = models.Oblast(
            province_id=province.province_id,
            name="Приуральская нефтегазоносная область",
            description="Территория концентрации месторождений Шаимского района.",
        )
        db.add(oblast); db.flush()

        rayon = models.Rayon(
            oblast_id=oblast.oblast_id,
            name="Шаимский нефтеносный район",
            description="Основной объект апробации системы.",
        )
        db.add(rayon); db.flush()

        # ВАЖНО: в полях месторождений, пласта и залежи только значения, которые
        # удалось подтвердить источником; остальное оставлено пустым (NULL), а не
        # выдумано. Пустые поля интерфейс не показывает, а чат-бот не домысливает.
        # Источники указаны в описании и в README (раздел «Откуда данные»).
        shaimskoe = models.Field(
            rayon_id=rayon.rayon_id, name="Шаимское",
            discovery_date=date(1960, 6, 21),
            coordinates=None,  # надёжных координат не найдено — заполните вручную (см. README)
            description=(
                "Первое промышленное месторождение нефти Западной Сибири (Трёхозёрное). "
                "Официальной датой открытия считается 21 июня 1960 г. (скважина Р-6), "
                "промышленная разработка с 1964 г. Источник: ЦДУ ТЭК (cdu.ru), 2020."
            ),
        )
        shushminskoe = models.Field(
            rayon_id=rayon.rayon_id, name="Шушминское",
            discovery_date=date(1985, 1, 1),  # известен только год
            coordinates="61.166667,64.133333",
            description=(
                "Открыто в 1985 г.; по запасам мелкое, по геологическому строению сложное. "
                "Источник: каталог НефтьГазИнформ (oilgasinform.ru)."
            ),
        )
        lovinskoe = models.Field(
            rayon_id=rayon.rayon_id, name="Ловинское",
            discovery_date=date(1973, 1, 1),  # известен только год
            coordinates="61.25,65.00",
            description="Открыто в 1973 г. скважиной № 31. Источник: nftn.ru (каталог месторождений).",
        )
        db.add_all([shaimskoe, shushminskoe, lovinskoe]); db.flush()

        horizon_t1 = models.Horizon(
            field_id=shaimskoe.field_id, index_code="Т1", formation="тюменская",
        )
        db.add(horizon_t1); db.flush()

        deposit = models.Deposit(
            horizon_id=horizon_t1.horizon_id, name="Шаимская залежь",
            oil_type="лёгкая",  # удельный вес шаимской нефти 0,819–0,843 (ЦДУ ТЭК)
        )
        db.add(deposit); db.flush()

        rt_geo = models.ResearchType(name="Геологическое", description="Исследования геологического строения")
        rt_geofiz = models.ResearchType(name="Геофизическое", description="Каротаж, сейсморазведка")
        rt_gidro = models.ResearchType(name="Гидрогеологическое", description="Изучение подземных вод")
        rt_geodin = models.ResearchType(name="Геодинамическое", description="Анализ тектонических процессов")
        db.add_all([rt_geo, rt_geofiz, rt_gidro, rt_geodin]); db.flush()

        a_ivanov = models.Author(last_name="Иванов", first_name="Иван", middle_name="Иванович",
                                  affiliation="Тюменский индустриальный университет")
        a_petrov = models.Author(last_name="Петров", first_name="Алексей", middle_name="Алексеевич")
        a_sidorov = models.Author(last_name="Сидоров", first_name="Андрей", middle_name="Андреевич")
        a_kuznetsov = models.Author(last_name="Кузнецов", first_name="Дмитрий", middle_name="Николаевич")
        a_ivanova = models.Author(last_name="Иванова", first_name="Елена", middle_name="Сергеевна")
        db.add_all([a_ivanov, a_petrov, a_sidorov, a_kuznetsov, a_ivanova]); db.flush()

        pubs_data = [
            dict(title="Геологическое строение Шаимского района", year=2005,
                 journal="Геология нефти и газа", link="https://example.ru",
                 author=a_ivanov, research_type=rt_geo, field=shaimskoe),
            dict(title="Геофизические исследования Шаимского месторождения", year=2010,
                 journal="Нефтяное хозяйство", link="https://example.ru/sidorov2010",
                 author=a_sidorov, research_type=rt_geofiz, field=shaimskoe),
            dict(title="Строение и нефтегазоносность Шаимского района", year=2015,
                 journal="Геология нефти и газа", link="https://example.ru/petrov2015",
                 author=a_petrov, research_type=rt_geo, field=shaimskoe),
            dict(title="Геодинамические особенности пласта Т1", year=2018,
                 journal="Геология и геофизика", link="https://example.ru/kuznetsov2018",
                 author=a_kuznetsov, research_type=rt_geodin, field=None, horizon=horizon_t1),
            dict(title="Гидрогеологические условия среднеюрского комплекса", year=2020,
                 journal="Геология нефти и газа", link="https://example.ru/ivanova2020",
                 author=a_ivanova, research_type=rt_gidro, field=None, deposit=deposit),
        ]

        for data in pubs_data:
            pub = models.Publication(
                title=data["title"], year=data["year"], journal=data["journal"],
                type="статья", link=data["link"],
            )
            db.add(pub); db.flush()

            db.add(models.PublicationAuthor(
                publication_id=pub.publication_id, author_id=data["author"].author_id,
                author_role="автор", author_order=1,
            ))
            db.add(models.PublicationResearchType(
                publication_id=pub.publication_id, type_id=data["research_type"].type_id,
            ))

            if data.get("field"):
                db.add(models.PublicationField(
                    publication_id=pub.publication_id, field_id=data["field"].field_id, relevance_score=1.0,
                ))
            if data.get("horizon"):
                db.add(models.PublicationHorizon(
                    publication_id=pub.publication_id, horizon_id=data["horizon"].horizon_id, relevance_score=1.0,
                ))
            if data.get("deposit"):
                db.add(models.PublicationDeposit(
                    publication_id=pub.publication_id, deposit_id=data["deposit"].deposit_id, relevance_score=1.0,
                ))

        db.commit()

        print(
            f"Готово: {db.query(models.Province).count()} провинция(и), "
            f"{db.query(models.Field).count()} месторождения, "
            f"{db.query(models.Publication).count()} публикаций, "
            f"{db.query(models.Author).count()} авторов."
        )
    finally:
        db.close()


if __name__ == "__main__":
    if engine.dialect.name != "sqlite" and "--yes" not in sys.argv:
        print(f"ВНИМАНИЕ: скрипт УДАЛИТ все таблицы и данные в базе {engine.url.render_as_string(hide_password=True)} "
              "и создаст демо-данные.")
        if input("Введите YES, чтобы продолжить: ").strip() != "YES":
            sys.exit("Отменено, база не тронута.")
    run()
