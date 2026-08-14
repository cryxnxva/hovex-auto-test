"""Точка входа парсера Avito.

Оркестрация: для каждого артикула из конфигурации выполняется поиск на
Avito (живой запрос с graceful degradation на локальные HTML-файлы из
``test_data``), затем — фильтрация, сортировка и сохранение пяти самых
дешёвых объявлений в ``result.xlsx``.

Компоненты:
- :class:`AvitoClient` — получение HTML-выдачи (живой запрос / локальный файл);
- :class:`SearchPipeline` — фильтрация, ранжирование и сборка строк результата;
- :class:`ResultExporter` — формирование DataFrame и экспорт в Excel.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pandas as pd
import requests

from config import AppConfig, SearchArticle
from parser import AdvertCard, AvitoPageParser

logger = logging.getLogger(__name__)


class SearchStatus(str, Enum):
    """Статус поиска по одному артикулу, отображаемый в итоговой таблице."""

    OK = "ok"
    NOT_FOUND = "не найдено"
    ERROR = "ошибка"


@dataclass(frozen=True)
class ResultRow:
    """Одна строка итоговой таблицы (одно объявление либо строка-статус)."""

    article: str
    search_query: str
    title: str | None
    price: int | None
    location: str | None
    condition: str | None
    url: str | None
    price_rank: int | None
    check_date: str
    status: str


class AvitoClient:
    """Источник HTML-выдачи Avito с graceful degradation.

    Сначала пытается получить страницу поиска напрямую. Если запрос
    неудачен (сетевая ошибка, статус не 200, капча) либо в ответе нет
    карточек объявлений (анти-бот страница вместо выдачи) — читает
    локальный файл ``test_data/{артикул}.html``.
    """

    # Маркеры капчи: редирект на captcha-эндпоинт либо data-marker в разметке.
    _CAPTCHA_MARKERS: tuple[str, ...] = ('data-marker="captcha"', "avito-captcha")
    # Маркеры карточек объявлений в HTML выдачи.
    _CARD_MARKERS: tuple[str, ...] = (
        'data-marker="item"',
        'data-marker="item-title"',
        'itemprop="item"',
    )
    # Маркеры легитимной пустой выдачи («ничего не найдено»).
    _EMPTY_MARKERS: tuple[str, ...] = ('data-marker="empty-state"', "ничего не найдено")

    def __init__(self, config: AppConfig) -> None:
        self._config = config

    def fetch_html(self, article: SearchArticle) -> str | None:
        """Вернуть HTML выдачи по артикулу или None, если источник недоступен."""
        if self._config.use_live_requests:
            live_html = self._fetch_live(article)
            if live_html is not None:
                return live_html
        else:
            logger.info("Сетевые запросы отключены, читаю локальный файл")
        return self._read_local(article)

    def _fetch_live(self, article: SearchArticle) -> str | None:
        """Живой запрос к Avito. Возвращает HTML или None при любой неудаче."""
        url = self._config.build_search_url(article.search_query)
        logger.info("Запрос к Avito: %s", url)
        try:
            response = requests.get(
                url, headers=self._config.headers, timeout=self._config.timeout_seconds
            )
        except requests.RequestException as exc:
            logger.warning("Сетевой запрос не удался: %s", exc)
            return None
        if response.status_code != 200:
            logger.warning("Avito вернул HTTP %s, переключаюсь на локальный файл", response.status_code)
            return None
        if self._is_captcha(response):
            logger.warning("Обнаружена капча Avito, переключаюсь на локальный файл")
            return None
        if self._is_empty(response.text):
            # Легитимный ответ: по запросу действительно ничего не найдено.
            # Возвращаем пустой HTML, чтобы получить статус «не найдено».
            logger.info("Avito вернул пустую выдачу (ничего не найдено)")
            return ""
        if not self._has_cards(response.text):
            logger.warning(
                "Живой ответ не содержит карточек объявлений, переключаюсь на локальный файл"
            )
            return None
        logger.info("Живой запрос успешен (HTTP %s)", response.status_code)
        return response.text

    def _read_local(self, article: SearchArticle) -> str | None:
        """Прочитать локальный HTML-файл выдачи для артикула."""
        path = self._config.test_data_dir / f"{article.code}.html"
        if not path.is_file():
            logger.warning("Локальный файл не найден: %s", path)
            return None
        logger.info("Читаю локальный файл: %s", path)
        try:
            data = path.read_bytes()
        except OSError as exc:
            logger.error("Не удалось прочитать файл %s: %s", path, exc)
            return None
        return self._decode(data)

    @staticmethod
    def _is_captcha(response: requests.Response) -> bool:
        """Определить, является ли ответ Avito страницей капчи."""
        url = response.url.lower()
        if "captcha" in url:
            return True
        body = response.text.lower()
        return any(marker in body for marker in AvitoClient._CAPTCHA_MARKERS)

    @staticmethod
    def _is_empty(html: str) -> bool:
        """Содержит ли HTML маркеры пустой выдачи («ничего не найдено»)."""
        body = html.lower()
        return any(marker in body for marker in AvitoClient._EMPTY_MARKERS)

    @staticmethod
    def _has_cards(html: str) -> bool:
        """Содержит ли HTML маркеры карточек объявлений (признак выдачи)."""
        return any(marker in html for marker in AvitoClient._CARD_MARKERS)

    @staticmethod
    def _decode(data: bytes) -> str:
        """Декодировать HTML: сначала UTF-8, затем cp1251, иначе с заменой."""
        for encoding in ("utf-8", "cp1251"):
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", errors="replace")


class SearchPipeline:
    """Пайплайн обработки одного артикула: данные -> строки результата.

    Логика: получить HTML -> распарсить карточки -> отфильтровать
    (новое, Москва/МО, числовая цена) -> отсортировать по цене ->
    убрать дубли по ссылке -> оставить N самых дешёвых -> проранжировать.
    """

    def __init__(self, config: AppConfig, parser: AvitoPageParser) -> None:
        self._config = config
        self._parser = parser
        self._client = AvitoClient(config)

    def process(self, article: SearchArticle) -> list[ResultRow]:
        """Обработать артикул и вернуть строки для итоговой таблицы."""
        check_date = self._now()
        html = self._safe_fetch(article)
        if html is None:
            return [self._status_row(article, check_date, SearchStatus.ERROR)]
        cards = self._safe_parse(html)
        if cards is None:
            return [self._status_row(article, check_date, SearchStatus.ERROR)]
        matches = self._select_matches(cards)
        if not matches:
            return [self._status_row(article, check_date, SearchStatus.NOT_FOUND)]
        return self._to_rows(article, matches, check_date)

    def _safe_fetch(self, article: SearchArticle) -> str | None:
        """Получить HTML, любая ошибка источника превращается в None."""
        try:
            return self._client.fetch_html(article)
        except Exception as exc:  # noqa: BLE001 - граница модуля
            logger.exception("Ошибка получения данных по артикулу %s: %s", article.code, exc)
            return None

    def _safe_parse(self, html: str) -> list[AdvertCard] | None:
        """Распарсить HTML, ошибка парсинга превращается в None."""
        try:
            return self._parser.parse(html)
        except Exception as exc:  # noqa: BLE001 - граница модуля
            logger.exception("Ошибка парсинга HTML: %s", exc)
            return None

    def _select_matches(self, cards: list[AdvertCard]) -> list[AdvertCard]:
        """Отфильтровать, отсортировать, убрать дубли и взять N самых дешёвых."""
        matches = [card for card in cards if self._passes_filters(card)]
        matches.sort(key=lambda card: card.price)
        deduped = self._dedupe_by_url(matches)
        return deduped[: self._config.max_results_per_article]

    def _passes_filters(self, card: AdvertCard) -> bool:
        """Все условия отбора: новое, Москва/МО, числовая цена."""
        if card.price is None:
            logger.debug("Отсеяно (нет цены): %s", card.title)
            return False
        if not self._condition_matches(card.condition):
            logger.debug("Отсеяно (не «Новое»): %s", card.title)
            return False
        if not self._location_matches(card.location):
            logger.debug("Отсеяно (не Москва/МО): %s", card.title)
            return False
        return True

    def _condition_matches(self, condition: str) -> bool:
        """Проверить, что состояние товара входит в допустимый список.

        Значение берётся после двоеточия и сравнивается целиком, чтобы
        «Как новое» не проходило по подстроке «Новое».
        """
        value = condition.lower().split(":", 1)[-1].strip()
        return any(value == allowed.lower() for allowed in self._config.allowed_conditions)

    def _location_matches(self, location: str) -> bool:
        """Проверить, что локация попадает в Москву или Московскую область.

        Учитывается только первый сегмент (город/регион до запятой) с
        префиксом «г. » — точным сравнением с полными названиями. Так
        «Московский проспект, Санкт-Петербург» не проходит фильтр.
        """
        first = location.lower().split(",", 1)[0].strip()
        first = first.removeprefix("г. ").strip()
        return any(first == keyword for keyword in self._config.allowed_location_keywords)

    @staticmethod
    def _normalize_url(url: str) -> str:
        """Нормализовать ссылку для дедупликации: без query, фрагмента и слеша."""
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))

    @staticmethod
    def _dedupe_by_url(cards: list[AdvertCard]) -> list[AdvertCard]:
        """Убрать дубли по ссылке, сохраняя первое (самое дешёвое) вхождение."""
        seen: set[str] = set()
        result: list[AdvertCard] = []
        for card in cards:
            key = SearchPipeline._normalize_url(card.url)
            if key in seen:
                logger.debug("Пропущен дубль по ссылке: %s", card.url)
                continue
            seen.add(key)
            result.append(card)
        return result

    def _to_rows(self, article: SearchArticle, matches: list[AdvertCard], check_date: str) -> list[ResultRow]:
        """Превратить отобранные объявления в строки с номером места по цене."""
        return [
            ResultRow(
                article=article.code,
                search_query=article.search_query,
                title=card.title,
                price=card.price,
                location=card.location,
                condition=card.condition,
                url=card.url,
                price_rank=rank,
                check_date=check_date,
                status=SearchStatus.OK.value,
            )
            for rank, card in enumerate(matches, start=1)
        ]

    def _status_row(self, article: SearchArticle, check_date: str, status: SearchStatus) -> ResultRow:
        """Строка-статус («не найдено»/«ошибка») с пустыми полями объявления."""
        return ResultRow(
            article=article.code,
            search_query=article.search_query,
            title=None,
            price=None,
            location=None,
            condition=None,
            url=None,
            price_rank=None,
            check_date=check_date,
            status=status.value,
        )

    @staticmethod
    def _now() -> str:
        """Текущая дата и время в формате ``YYYY-MM-DD HH:MM:SS``."""
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class ResultExporter:
    """Сборка pandas.DataFrame из строк результата и экспорт в Excel."""

    COLUMNS: tuple[str, ...] = (
        "article",
        "search_query",
        "title",
        "price",
        "location",
        "condition",
        "url",
        "price_rank",
        "check_date",
        "status",
    )

    def build_dataframe(self, rows: list[ResultRow]) -> pd.DataFrame:
        """Построить DataFrame с фиксированным порядком столбцов."""
        if not rows:
            return pd.DataFrame(columns=self.COLUMNS)
        return pd.DataFrame([asdict(row) for row in rows], columns=self.COLUMNS)

    def export(self, rows: list[ResultRow], path: Path) -> None:
        """Записать результат в файл Excel (движок openpyxl)."""
        dataframe = self.build_dataframe(rows)
        dataframe.to_excel(path, index=False, engine="openpyxl")
        logger.info("Результат сохранён в %s (%d строк)", path, len(dataframe))


def configure_logging() -> None:
    """Настроить корневой логгер с единым форматом."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main() -> int:
    """Запустить поиск по всем артикулам и сохранить результат."""
    configure_logging()
    config = AppConfig()
    parser = AvitoPageParser()
    pipeline = SearchPipeline(config, parser)
    exporter = ResultExporter()

    rows: list[ResultRow] = []
    for article in config.articles:
        logger.info("Поиск по артикулу %s (%s)", article.code, article.description)
        rows.extend(pipeline.process(article))

    try:
        exporter.export(rows, config.output_file)
    except OSError as exc:
        logger.error("Не удалось сохранить результат в %s: %s", config.output_file, exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
