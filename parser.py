"""Извлечение данных из HTML-выдачи Avito.

Парсер устойчив к изменениям вёрстки: каждый атрибут объявления
извлекается через цепочку селекторов с fallback. Основные ориентиры —
микроданные schema.org (``itemprop``) и data-marker'ы Avito.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)

# Собираем все группы цифр, отбрасывая пробелы и символ валюты.
_DIGITS_RE = re.compile(r"\d+")


@dataclass(frozen=True)
class AdvertCard:
    """Одно объявление, извлечённое из страницы выдачи.

    Attributes:
        title: Заголовок объявления.
        price: Цена в рублях или None, если число определить не удалось.
        location: Город/регион объявления.
        condition: Сырой текст блока состояния товара.
        url: Абсолютная ссылка на объявление.
    """

    title: str
    price: int | None
    location: str
    condition: str
    url: str


class AvitoPageParser:
    """Парсер страницы выдачи Avito в список :class:`AdvertCard`.

    Args:
        base_url: Базовый URL для нормализации относительных ссылок.
    """

    # Селекторы контейнеров карточек: data-marker'ы Avito, затем микроданные.
    _CARD_SELECTORS: tuple[str, ...] = (
        'div[data-marker="item"]',
        'div[itemprop="item"]',
    )
    # Селекторы заголовков-ссылок (fallback, если контейнеры не найдены).
    _TITLE_LINK_SELECTORS: tuple[str, ...] = (
        'a[data-marker="item-title"]',
        'a[itemprop="url"]',
    )

    def __init__(self, base_url: str = "https://www.avito.ru") -> None:
        self._base_url = base_url

    def parse(self, html: str) -> list[AdvertCard]:
        """Разобрать HTML-страницу выдачи и вернуть список объявлений.

        Args:
            html: HTML-код страницы выдачи.

        Returns:
            Список валидных объявлений. Пустой/битый HTML даёт пустой список.
        """
        if not html or not html.strip():
            logger.warning("Получен пустой HTML-документ")
            return []
        soup = BeautifulSoup(html, "html.parser")
        containers = self._find_card_containers(soup)
        if not containers:
            logger.warning("На странице не найдены карточки объявлений")
            return []
        cards = (self._parse_card(node) for node in containers)
        return [card for card in cards if card is not None]

    def _find_card_containers(self, soup: BeautifulSoup) -> list[Tag]:
        """Определить DOM-узлы карточек: по селекторам или заголовкам-ссылкам."""
        for selector in self._CARD_SELECTORS:
            nodes = soup.select(selector)
            if nodes:
                return list(nodes)
        anchors = soup.select(", ".join(self._TITLE_LINK_SELECTORS))
        return [self._card_scope(anchor) for anchor in anchors]

    def _card_scope(self, anchor: Tag) -> Tag:
        """Найти контейнер карточки по ссылке-заголовку."""
        parent = anchor.find_parent(itemprop="item")
        return parent if parent is not None else anchor.parent

    def _parse_card(self, node: Tag) -> AdvertCard | None:
        """Извлечь одно объявление из узла карточки.

        Returns:
            :class:`AdvertCard` или None, если нет заголовка или ссылки.
        """
        title = self._extract_title(node)
        url = self._extract_url(node)
        if not title or not url:
            logger.debug("Пропущена карточка без заголовка или ссылки")
            return None
        return AdvertCard(
            title=title,
            price=self._extract_price(node),
            location=self._extract_location(node),
            condition=self._extract_condition(node),
            url=urljoin(self._base_url, url),
        )

    def _extract_title(self, node: Tag) -> str:
        """Заголовок: микроданные ``h3[itemprop="name"]`` либо текст ссылки."""
        selectors = ('h3[itemprop="name"]', *self._TITLE_LINK_SELECTORS)
        tag = self._first_select(node, selectors)
        if tag is None:
            return ""
        return self._normalize_text(tag.get_text(" ", strip=True))

    def _extract_url(self, node: Tag) -> str:
        """Ссылка: ``a[itemprop="url"]`` либо ``a[data-marker="item-title"]``."""
        return self._first_attr(node, self._TITLE_LINK_SELECTORS, "href")

    def _extract_price(self, node: Tag) -> int | None:
        """Цена: микроданные ``meta[itemprop="price"]`` либо блок цены.

        Блок цены пробуется и в том случае, когда из meta число извлечь
        не удалось (например, контент с десятичной дробью).
        """
        price = self._parse_price(
            self._first_attr(node, ('meta[itemprop="price"]',), "content")
        )
        if price is None:
            price_tag = self._first_select(node, ('div[data-marker="item-price"]',))
            if price_tag is not None:
                price = self._parse_price(price_tag.get_text(" ", strip=True))
        return price

    def _extract_location(self, node: Tag) -> str:
        """Локация: микроданные ``addressLocality`` либо блок адреса."""
        text = self._first_attr(node, ('meta[itemprop="addressLocality"]',), "content")
        if text:
            return text.strip()
        selectors = ('span[itemprop="addressLocality"]', '[data-marker="item-address"]')
        tag = self._first_select(node, selectors)
        if tag is None:
            return ""
        return self._normalize_text(tag.get_text(" ", strip=True))

    def _extract_condition(self, node: Tag) -> str:
        """Состояние товара: микроданные ``itemCondition`` либо параметры."""
        selectors = (
            'div[itemprop="itemCondition"]',
            'div[data-marker="item-specific-params"]',
        )
        tag = self._first_select(node, selectors)
        if tag is None:
            return ""
        return self._normalize_text(tag.get_text(" ", strip=True))

    def _first_select(self, node: Tag, selectors: Iterable[str]) -> Tag | None:
        """Вернуть первый подходящий по цепочке селекторов элемент или None."""
        for selector in selectors:
            tag = node.select_one(selector)
            if tag is not None:
                return tag
        return None

    def _first_attr(self, node: Tag, selectors: Iterable[str], attr: str) -> str:
        """Вернуть атрибут первого подходящего элемента или пустую строку."""
        tag = self._first_select(node, selectors)
        if tag is None:
            return ""
        value = tag.get(attr)
        return str(value) if value is not None else ""

    # Слова-маркеры составных/нечисловых цен, которые не трактуем как рубли.
    _UNIT_WORDS: tuple[str, ...] = ("млн", "тыс", "млрд")

    @staticmethod
    def _parse_price(text: str | None) -> int | None:
        """Превратить текстовую цену в целое число рублей.

        Берётся только целая часть до разделителя (``1200.50`` -> ``1200``),
        разделители групп цифр (``1 200``) отбрасываются. Составные цены
        (``1,5 млн ₽``) и значения без цифр возвращают None.
        """
        if not text:
            return None
        value = text.strip().lower()
        if any(word in value for word in AvitoPageParser._UNIT_WORDS):
            return None
        match = re.match(r"(\d[\d\s\u00a0]*\d|\d)(?=\D|$)", value)
        if match is None:
            return None
        digits = "".join(_DIGITS_RE.findall(match.group(1)))
        if not digits:
            return None
        try:
            return int(digits)
        except ValueError:
            return None

    @staticmethod
    def _normalize_text(text: str) -> str:
        """Схлопнуть пробелы (включая неразрывные) и обрезать края."""
        return re.sub(r"\s+", " ", text).strip()
