"""Seed the initial info/legal content pages with draft (болванка) copy.

Idempotent one-off: creates each page only if its slug does not already exist,
so re-running never clobbers copy a manager has since edited in the admin. This
is deliberately a *script*, not an Alembic data-migration — the content is
manager-editable, and baking editable rows into a migration would let a
downgrade / re-run overwrite live edits.

Run on the server after the backend is deployed:

    docker compose -f docker-compose.prod.yml exec app \\
        uv run python scripts/seed_content_pages.py

All copy below is a DRAFT ("черновик") and must be reviewed for legal accuracy
(MD/EU consumer law) and have its placeholders (company name / IDNO / address /
email / phone) filled in — from the admin panel — before launch.
"""

import asyncio

from app.core.db import session_factory
from app.schemas.content_page import ContentPageCreate, ContentPageTranslationIn
from app.services.content_page_service import (
    ContentPageConflictError,
    ContentPageService,
)

# Each entry: slug, position, and the ru/ro (title, body-markdown, seo_description).
_PAGES: list[dict] = [
    {
        "slug": "delivery",
        "position": 1,
        "ru": {
            "title": "Доставка и оплата",
            "seo_description": "Способы доставки по Молдове и оплата при получении в магазине evix.",
            "body": """> _Черновик — требует проверки._

## Доставка
- **Курьер по Кишинёву** — фиксированный тариф, 1–2 рабочих дня.
- **Курьер по Молдове** — тариф зависит от региона, 2–4 рабочих дня.
- **Самовывоз** — бесплатно из нашего пункта выдачи.

## Оплата
- **Наличными при получении** (наложенный платёж).
- Все цены указаны в молдавских леях (MDL).

## Обработка заказа
Заказы обрабатываются в рабочие дни. После оформления с вами свяжется оператор для подтверждения.
""",
        },
        "ro": {
            "title": "Livrare și plată",
            "seo_description": "Modalități de livrare în Moldova și plata la primire în magazinul evix.",
            "body": """> _Ciornă — necesită verificare._

## Livrare
- **Curier în Chișinău** — tarif fix, 1–2 zile lucrătoare.
- **Curier în Moldova** — tariful depinde de regiune, 2–4 zile lucrătoare.
- **Ridicare personală** — gratuit din punctul nostru de ridicare.

## Plată
- **Numerar la primire** (ramburs).
- Toate prețurile sunt indicate în lei moldovenești (MDL).

## Procesarea comenzii
Comenzile se procesează în zilele lucrătoare. După plasare, un operator vă va contacta pentru confirmare.
""",
        },
    },
    {
        "slug": "returns",
        "position": 2,
        "ru": {
            "title": "Возврат товара",
            "seo_description": "Право на возврат товара в течение 14 дней и условия возврата.",
            "body": """> _Черновик — требует юридической проверки._

## Право на возврат
Согласно законодательству о защите прав потребителей, вы имеете право вернуть товар в течение **14 дней** с момента получения без объяснения причин.

## Условия
- Товар не был в употреблении, сохранён товарный вид и оригинальная упаковка.
- Сохранены все ярлыки и комплектность.

## Как оформить возврат
Свяжитесь с нами (см. **Контакты**) и укажите номер заказа. Мы согласуем способ возврата.

## Возврат средств
Деньги возвращаются в течение 14 дней после получения возвращённого товара.

## Исключения
Возврату не подлежат товары персонального и гигиенического назначения, а также иные категории, предусмотренные законом.
""",
        },
        "ro": {
            "title": "Returul produsului",
            "seo_description": "Dreptul de a returna produsul în 14 zile și condițiile de retur.",
            "body": """> _Ciornă — necesită verificare juridică._

## Dreptul de retur
Conform legislației privind protecția consumatorilor, aveți dreptul să returnați produsul în termen de **14 zile** de la primire, fără a invoca vreun motiv.

## Condiții
- Produsul nu a fost utilizat, și-a păstrat aspectul comercial și ambalajul original.
- Sunt păstrate toate etichetele și componentele.

## Cum se face returul
Contactați-ne (vezi **Contacte**) și indicați numărul comenzii. Vom conveni modalitatea de retur.

## Rambursarea banilor
Banii se returnează în termen de 14 zile de la primirea produsului returnat.

## Excepții
Nu se acceptă la retur produsele de uz personal și igienic, precum și alte categorii prevăzute de lege.
""",
        },
    },
    {
        "slug": "privacy",
        "position": 3,
        "ru": {
            "title": "Политика конфиденциальности",
            "seo_description": "Какие данные собирает магазин evix и как они используются.",
            "body": """> _Черновик — требует проверки._

## Какие данные мы собираем
Для оформления заказа: имя, телефон и адрес доставки.

## Для чего
Только для обработки и доставки заказов и связи с вами по заказу.

## Аналитика
Мы используем собственную (first-party) аналитику посещаемости без сторонних трекеров. Мы не храним IP-адреса и не передаём ваши данные третьим сторонам, кроме служб доставки, необходимых для выполнения заказа.

## Хранение
Данные хранятся столько, сколько необходимо для выполнения заказа и соблюдения требований законодательства.

## Ваши права
Вы можете запросить доступ, исправление или удаление ваших данных — напишите нам (см. **Контакты**).
""",
        },
        "ro": {
            "title": "Politica de confidențialitate",
            "seo_description": "Ce date colectează magazinul evix și cum sunt utilizate.",
            "body": """> _Ciornă — necesită verificare._

## Ce date colectăm
Pentru plasarea comenzii: nume, telefon și adresa de livrare.

## În ce scop
Doar pentru procesarea și livrarea comenzilor și pentru a vă contacta în legătură cu comanda.

## Analiză
Folosim o analiză proprie (first-party) a traficului, fără trackere terțe. Nu stocăm adrese IP și nu transmitem datele dumneavoastră terților, cu excepția serviciilor de curierat necesare pentru livrarea comenzii.

## Stocare
Datele se păstrează atât timp cât este necesar pentru executarea comenzii și respectarea cerințelor legale.

## Drepturile dumneavoastră
Puteți solicita accesul, corectarea sau ștergerea datelor — scrieți-ne (vezi **Contacte**).
""",
        },
    },
    {
        "slug": "terms",
        "position": 4,
        "ru": {
            "title": "Публичная оферта",
            "seo_description": "Условия покупки и публичная оферта магазина evix.",
            "body": """> _Черновик — требует юридической проверки._

## Общие положения
Настоящие условия регулируют отношения между магазином **evix** (Продавец) и покупателем.

## Заказ
Оформление заказа означает согласие с настоящими условиями. Продавец подтверждает заказ по телефону.

## Цены и оплата
Все цены указаны в MDL. Оплата — наличными при получении.

## Обязанности сторон
Продавец обязуется передать товар надлежащего качества. Покупатель обязуется принять и оплатить заказ.

## Разрешение споров
Споры разрешаются в соответствии с законодательством Республики Молдова.
""",
        },
        "ro": {
            "title": "Oferta publică",
            "seo_description": "Condițiile de cumpărare și oferta publică a magazinului evix.",
            "body": """> _Ciornă — necesită verificare juridică._

## Dispoziții generale
Prezentele condiții reglementează relațiile dintre magazinul **evix** (Vânzător) și cumpărător.

## Comanda
Plasarea comenzii înseamnă acceptul prezentelor condiții. Vânzătorul confirmă comanda telefonic.

## Prețuri și plată
Toate prețurile sunt în MDL. Plata — numerar la primire.

## Obligațiile părților
Vânzătorul se obligă să predea un produs de calitate corespunzătoare. Cumpărătorul se obligă să preia și să achite comanda.

## Soluționarea litigiilor
Litigiile se soluționează în conformitate cu legislația Republicii Moldova.
""",
        },
    },
    {
        "slug": "contacts",
        "position": 5,
        "ru": {
            "title": "Контакты",
            "seo_description": "Контакты и реквизиты магазина evix.",
            "body": """> _Черновик — заполните реквизиты._

## Контакты
- **Компания:** _[название юр. лица]_
- **IDNO:** _[IDNO]_
- **Адрес:** _[адрес]_
- **Email:** _[email]_
- **Телефон:** _[телефон]_
- **Часы работы:** Пн–Пт, 09:00–18:00
""",
        },
        "ro": {
            "title": "Contacte",
            "seo_description": "Contacte și date de identificare ale magazinului evix.",
            "body": """> _Ciornă — completați datele._

## Contacte
- **Compania:** _[denumirea persoanei juridice]_
- **IDNO:** _[IDNO]_
- **Adresa:** _[adresa]_
- **Email:** _[email]_
- **Telefon:** _[telefon]_
- **Program:** Lun–Vin, 09:00–18:00
""",
        },
    },
    {
        "slug": "about",
        "position": 6,
        "ru": {
            "title": "О нас",
            "seo_description": "О магазине evix — товары для дома, авто и техники с доставкой по Молдове.",
            "body": """> _Черновик._

## О магазине evix
**evix** — интернет-магазин товаров для дома, автотоваров, техники, красоты и здоровья с доставкой по всей Молдове. Мы предлагаем выгодные цены, оплату при получении и удобную доставку.
""",
        },
        "ro": {
            "title": "Despre noi",
            "seo_description": "Despre magazinul evix — produse pentru casă, auto și tehnică cu livrare în Moldova.",
            "body": """> _Ciornă._

## Despre magazinul evix
**evix** este un magazin online de produse pentru casă, auto, tehnică, frumusețe și sănătate, cu livrare în toată Moldova. Oferim prețuri avantajoase, plata la primire și livrare comodă.
""",
        },
    },
]


def _payload(page: dict) -> ContentPageCreate:
    """Build a validated create payload from a seed entry."""
    return ContentPageCreate(
        slug=page["slug"],
        is_published=True,
        show_in_footer=True,
        position=page["position"],
        translations=[
            ContentPageTranslationIn(lang=lang, **page[lang]) for lang in ("ru", "ro")
        ],
    )


async def main() -> None:
    """Create each seed page whose slug is not already present."""
    created, skipped = 0, 0
    async with session_factory() as session:
        service = ContentPageService(session)
        existing = {page.slug for page in await service.list_pages()}
        for page in _PAGES:
            if page["slug"] in existing:
                print(f"skip (exists): {page['slug']}")
                skipped += 1
                continue
            try:
                await service.create_page(_payload(page))
                print(f"created: {page['slug']}")
                created += 1
            except ContentPageConflictError:
                print(f"skip (conflict): {page['slug']}")
                skipped += 1
    print(f"done: created={created} skipped={skipped}")


if __name__ == "__main__":
    asyncio.run(main())
