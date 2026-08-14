"""Конфигурация поисковых запросов и параметров работы парсера Avito.

Вся конфигурация описывается типизированными frozen dataclass'ами и не
содержит глобальных переменных: экземпляр :class:`AppConfig` создаётся
в точке входа ``main.py`` и явно прокидывается в компоненты.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote


@dataclass(frozen=True)
class SearchArticle:
    """Один артикул товара HONEX, по которому выполняется поиск на Avito.

    Attributes:
        code: Артикул товара, например ``21830S1400``.
        description: Человекочитаемое название товара.
    """

    code: str
    description: str

    @property
    def search_query(self) -> str:
        """Поисковый запрос для Avito — сам артикул."""
        return self.code


@dataclass(frozen=True)
class AppConfig:
    """Настройки парсера: источники данных, фильтры и вывод.

    Значения по умолчанию подобраны под требования ТЗ: поиск по всей
    России с последующей фильтрацией по Москве и Московской области,
    отбор только новых товаров и сохранение 5 самых дешёвых на артикул.
    """

    region: str = "rossiya"
    """URL-слаг региона для построения поисковой ссылки Avito."""
    articles: tuple[SearchArticle, ...] = (
        SearchArticle("21830S1400", "Кронштейн КПП в сборе"),
        SearchArticle("244203A000", "Натяжитель цепи масляного насоса"),
    )
    """Артикулы, по которым выполняется поиск."""
    headers: dict[str, str] = field(
        default_factory=lambda: {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        }
    )
    """HTTP-заголовки для прямого запроса к Avito (без секретов)."""
    timeout_seconds: float = 15.0
    """Таймаут HTTP-запроса в секундах."""
    max_results_per_article: int = 5
    """Сколько самых дешёвых объявлений сохранять по каждому артикулу."""
    allowed_conditions: tuple[str, ...] = ("Новое",)
    """Допустимые состояния товара (проверка регистронезависимая)."""
    allowed_location_keywords: tuple[str, ...] = ("москв", "москов", "подмосков")
    """Подстроки локации: ``москв`` — Москва, ``москов`` — Московская область,
    ``подмосков`` — Подмосковье. Проверка регистронезависимая."""
    test_data_dir: Path = Path("test_data")
    """Папка с локальными HTML-файлами для graceful degradation."""
    output_file: Path = Path("result.xlsx")
    """Файл-результат (Excel)."""

    def __post_init__(self) -> None:
        """Проверить, что конфигурация валидна на этапе создания."""
        if not self.articles:
            raise ValueError("Список артикулов не может быть пустым")
        if self.max_results_per_article <= 0:
            raise ValueError("max_results_per_article должен быть больше нуля")

    def build_search_url(self, query: str) -> str:
        """Собрать URL поисковой выдачи Avito по артикулу.

        Args:
            query: Поисковый запрос (артикул товара).

        Returns:
            Полный URL страницы выдачи Avito.
        """
        return f"https://www.avito.ru/{self.region}?q={quote(query)}"
