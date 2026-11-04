"""Запасная копия основы онтологии (классы, свойства, аксиомы из data/oil-gas-ontology.owl, БЕЗ экземпляров).
Используется, только если файл онтологии не найден. Источник истины — файл: при его изменении в Protégé
приложение берёт структуру из него."""

FALLBACK_TTL = r"""@prefix ontology: <http://west-siberia-ontology.org/ontology#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

<http://west-siberia-ontology.org/ontology> a owl:Ontology .

ontology:Геодинамическое a owl:Class ;
    rdfs:subClassOf ontology:Исследование .

ontology:Геокриологическое a owl:Class ;
    rdfs:subClassOf ontology:Исследование .

ontology:Геологическое a owl:Class ;
    rdfs:subClassOf ontology:Исследование .

ontology:Геофизическое a owl:Class ;
    rdfs:subClassOf ontology:Исследование .

ontology:Гидрогеологическое a owl:Class ;
    rdfs:subClassOf ontology:Исследование .

ontology:Залежь a owl:Class ;
    rdfs:subClassOf ontology:НефтегазовыйОбъект .

ontology:ЗонаНефтегазонакопления a owl:Class ;
    rdfs:subClassOf ontology:НефтегазовыйОбъект .

ontology:Месторождение a owl:Class ;
    rdfs:subClassOf ontology:НефтегазовыйОбъект .

ontology:Область a owl:Class ;
    rdfs:subClassOf ontology:НефтегазовыйОбъект .

ontology:Пласт a owl:Class ;
    rdfs:subClassOf ontology:НефтегазовыйОбъект .

ontology:Провинция a owl:Class ;
    rdfs:subClassOf ontology:НефтегазовыйОбъект .

ontology:Разработка a owl:Class ;
    rdfs:subClassOf ontology:Исследование .

ontology:Район a owl:Class ;
    rdfs:subClassOf ontology:НефтегазовыйОбъект .

ontology:автор a owl:DatatypeProperty ;
    rdfs:domain ontology:Публикация .

ontology:входитВ a owl:ObjectProperty,
        owl:TransitiveProperty ;
    rdfs:domain ontology:НефтегазовыйОбъект ;
    rdfs:range ontology:НефтегазовыйОбъект ;
    rdfs:subPropertyOf owl:topObjectProperty ;
    owl:inverseOf ontology:содержит .

ontology:год a owl:DatatypeProperty ;
    rdfs:domain ontology:Публикация .

ontology:журнал a owl:DatatypeProperty ;
    rdfs:domain ontology:Публикация .

ontology:имеетЗалежь a owl:ObjectProperty ;
    rdfs:subPropertyOf owl:topObjectProperty .

ontology:имеетМесторождение a owl:ObjectProperty ;
    rdfs:subPropertyOf owl:topObjectProperty .

ontology:имеетПласт a owl:ObjectProperty ;
    rdfs:subPropertyOf owl:topObjectProperty .

ontology:название a owl:DatatypeProperty ;
    rdfs:domain ontology:Публикация .

ontology:относитсяКОбъекту a owl:ObjectProperty ;
    rdfs:domain ontology:Публикация ;
    rdfs:range ontology:НефтегазовыйОбъект ;
    rdfs:subPropertyOf owl:topObjectProperty .

ontology:относитсяКТипуИсследования a owl:ObjectProperty ;
    rdfs:domain ontology:Публикация ;
    rdfs:range ontology:Исследование ;
    rdfs:subPropertyOf owl:topObjectProperty .

ontology:ссылка a owl:DatatypeProperty ;
    rdfs:domain ontology:Публикация .

ontology:содержит a owl:ObjectProperty ;
    rdfs:domain ontology:НефтегазовыйОбъект ;
    rdfs:range ontology:НефтегазовыйОбъект .

ontology:Исследование a owl:Class .

ontology:Публикация a owl:Class .

ontology:НефтегазовыйОбъект a owl:Class .

"""
