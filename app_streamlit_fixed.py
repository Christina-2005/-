import re
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

import pandas as pd
import requests
import streamlit as st
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


COUNTRY = "ru"
MAX_PAGES = 10
REQUEST_TIMEOUT = 15

REVIEW_COLUMNS = [
    "review_id",
    "app_id",
    "author",
    "title",
    "review",
    "rating",
    "date",
    "app_version",
]


class AppStoreScraperError(Exception):
    pass


class InvalidAppInputError(AppStoreScraperError):
    pass


class AppNotFoundError(AppStoreScraperError):
    pass


class AppStoreRequestError(AppStoreScraperError):
    pass


class AppStoreResponseError(AppStoreScraperError):
    pass


def extract_app_id(value):
    if value is None:
        raise InvalidAppInputError(
            "Введите ссылку App Store или ID приложения."
        )

    text = str(value).strip()

    if not text:
        raise InvalidAppInputError(
            "Введите ссылку App Store или ID приложения."
        )

    if text.isdigit():
        return text

    candidate = text if "://" in text else f"https://{text}"
    parsed = urlparse(candidate)
    host = parsed.netloc.lower().split(":", 1)[0]

    if host not in {"apps.apple.com", "itunes.apple.com"}:
        raise InvalidAppInputError(
            "Введите корректную ссылку App Store "
            "или числовой ID приложения."
        )

    match = re.search(r"/id(\d+)(?:/|$)", parsed.path)

    if not match:
        raise InvalidAppInputError(
            "В ссылке не найден ID приложения вида /id123456789."
        )

    return match.group(1)


def build_session():
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=0.7,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        raise_on_status=False,
    )

    adapter = HTTPAdapter(max_retries=retry)

    session = requests.Session()
    session.mount("https://", adapter)

    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 AppStoreReviewScraper/1.0"
            ),
            "Accept": "*/*",
        }
    )

    return session


def request_response(session, url, params=None):
    try:
        response = session.get(
            url,
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        return response

    except requests.RequestException as exc:
        raise AppStoreRequestError(
            f"Ошибка запроса к App Store: {exc}"
        ) from exc


def request_json(session, url, params=None):
    response = request_response(
        session,
        url,
        params=params,
    )

    try:
        data = response.json()
    except ValueError as exc:
        raise AppStoreResponseError(
            "App Store вернул ответ, который не является корректным JSON."
        ) from exc

    if not isinstance(data, dict):
        raise AppStoreResponseError(
            "App Store вернул JSON неожиданного формата."
        )

    return data


def validate_app_exists(app_id, session):
    data = request_json(
        session,
        "https://itunes.apple.com/lookup",
        params={
            "id": app_id,
            "country": COUNTRY,
        },
    )

    results = data.get("results", [])

    if not isinstance(results, list):
        raise AppStoreResponseError(
            "Apple Lookup API вернул неожиданный ответ."
        )

    matching_apps = [
        item
        for item in results
        if isinstance(item, dict)
        and str(item.get("trackId", "")) == str(app_id)
        and item.get("wrapperType") == "software"
    ]

    if not matching_apps:
        raise AppNotFoundError(
            f"Приложение с ID {app_id} "
            "не найдено в российском App Store."
        )

    return matching_apps[0]


def get_label(value):
    if isinstance(value, dict):
        label = value.get("label", "")
        return "" if label is None else str(label)

    return ""


def parse_json_review(entry, app_id):
    if not isinstance(entry, dict):
        return None

    if "im:rating" not in entry:
        return None

    rating_text = get_label(entry.get("im:rating"))

    try:
        rating = int(rating_text) if rating_text else None
    except ValueError:
        rating = None

    author_data = entry.get("author", {})
    if not isinstance(author_data, dict):
        author_data = {}

    return {
        "review_id": get_label(entry.get("id")),
        "app_id": str(app_id),
        "author": get_label(author_data.get("name")),
        "title": get_label(entry.get("title")),
        "review": get_label(entry.get("content")),
        "rating": rating,
        "date": get_label(entry.get("updated")),
        "app_version": get_label(entry.get("im:version")),
    }


def parse_json_feed(data, app_id):
    feed = data.get("feed")

    if not isinstance(feed, dict):
        raise AppStoreResponseError(
            "В JSON RSS отсутствует корректный объект feed."
        )

    entries = feed.get("entry", [])

    if entries is None:
        entries = []

    if isinstance(entries, dict):
        entries = [entries]

    if not isinstance(entries, list):
        raise AppStoreResponseError(
            "Поле entry в JSON RSS имеет неожиданный формат."
        )

    reviews = []

    for entry in entries:
        review = parse_json_review(entry, app_id)

        if review is not None:
            reviews.append(review)

    return reviews


ATOM_NS = "http://www.w3.org/2005/Atom"
ITUNES_NS = "http://itunes.apple.com/rss"


def xml_text(element, path, namespaces=None):
    found = element.find(path, namespaces or {})

    if found is None or found.text is None:
        return ""

    return found.text.strip()


def parse_xml_reviews(xml_text_data, app_id):
    try:
        root = ET.fromstring(xml_text_data)
    except ET.ParseError as exc:
        raise AppStoreResponseError(
            "XML RSS App Store имеет неожиданный формат."
        ) from exc

    namespaces = {
        "atom": ATOM_NS,
        "im": ITUNES_NS,
    }

    entries = root.findall("atom:entry", namespaces)

    reviews = []

    for entry in entries:
        rating_text = xml_text(
            entry,
            "im:rating",
            namespaces,
        )

        # Служебная запись приложения не является отзывом.
        if not rating_text:
            continue

        try:
            rating = int(rating_text)
        except ValueError:
            rating = None

        review_id = xml_text(
            entry,
            "atom:id",
            namespaces,
        )

        author = xml_text(
            entry,
            "atom:author/atom:name",
            namespaces,
        )

        title = xml_text(
            entry,
            "atom:title",
            namespaces,
        )

        review_body = ""

        for content in entry.findall("atom:content", namespaces):
            content_type = content.attrib.get("type", "")

            if content.text and content_type != "html":
                review_body = content.text.strip()
                break

        if not review_body:
            review_body = xml_text(
                entry,
                "atom:content",
                namespaces,
            )

        date = xml_text(
            entry,
            "atom:updated",
            namespaces,
        )

        app_version = xml_text(
            entry,
            "im:version",
            namespaces,
        )

        reviews.append(
            {
                "review_id": review_id,
                "app_id": str(app_id),
                "author": author,
                "title": title,
                "review": review_body,
                "rating": rating,
                "date": date,
                "app_version": app_version,
            }
        )

    return reviews


def rss_urls(app_id, page, output_format):
    """
    Пробуем два публичных варианта порядка сегментов URL Apple RSS.
    """

    return [
        (
            f"https://itunes.apple.com/{COUNTRY}/rss/customerreviews/"
            f"page={page}/id={app_id}/sortby=mostrecent/{output_format}"
        ),
        (
            f"https://itunes.apple.com/{COUNTRY}/rss/customerreviews/"
            f"id={app_id}/page={page}/sortby=mostrecent/{output_format}"
        ),
    ]


def fetch_review_page(session, app_id, page):
    """
    Основной источник — JSON RSS.
    Если он не вернул отзывы, пробуем XML RSS.
    """

    diagnostics = []
    valid_empty_response_seen = False

    for url in rss_urls(app_id, page, "json"):
        try:
            response = request_response(session, url)

            try:
                data = response.json()
            except ValueError:
                diagnostics.append(
                    f"JSON: ответ не является JSON ({url})"
                )
                continue

            if not isinstance(data, dict):
                diagnostics.append(
                    f"JSON: неожиданный формат ({url})"
                )
                continue

            try:
                reviews = parse_json_feed(data, app_id)
            except AppStoreResponseError as exc:
                diagnostics.append(f"JSON: {exc}")
                continue

            if reviews:
                return reviews, "Apple RSS JSON", diagnostics

            valid_empty_response_seen = True
            diagnostics.append(
                f"JSON: корректный ответ, но отзывов нет ({url})"
            )

        except AppStoreRequestError as exc:
            diagnostics.append(f"JSON: {exc}")

    for url in rss_urls(app_id, page, "xml"):
        try:
            response = request_response(session, url)

            try:
                reviews = parse_xml_reviews(
                    response.text,
                    app_id,
                )
            except AppStoreResponseError as exc:
                diagnostics.append(f"XML: {exc}")
                continue

            if reviews:
                return reviews, "Apple RSS XML", diagnostics

            valid_empty_response_seen = True
            diagnostics.append(
                f"XML: корректный ответ, но отзывов нет ({url})"
            )

        except AppStoreRequestError as exc:
            diagnostics.append(f"XML: {exc}")

    if valid_empty_response_seen:
        return [], "empty", diagnostics

    raise AppStoreRequestError(
        "Не удалось получить рабочий ответ ни от JSON-, "
        "ни от XML-версии Apple Customer Reviews RSS."
    )


def make_dedupe_key(review):
    review_id = str(review.get("review_id") or "").strip()

    if review_id:
        return ("review_id", review_id)

    return (
        "fallback",
        review.get("author", ""),
        review.get("title", ""),
        review.get("review", ""),
        review.get("rating"),
        review.get("date", ""),
        review.get("app_version", ""),
    )


def scrape_reviews(app_input):
    app_id = extract_app_id(app_input)
    session = build_session()

    reviews = []
    seen_reviews = set()
    sources_used = set()
    diagnostics = []

    status_info = {
        "rss_status": "unknown",
        "sources_used": [],
        "diagnostics": diagnostics,
    }

    try:
        # Проверяем существование приложения независимо от RSS.
        app_info = validate_app_exists(
            app_id,
            session,
        )

        for page in range(1, MAX_PAGES + 1):
            page_reviews, source, page_diagnostics = fetch_review_page(
                session,
                app_id,
                page,
            )

            diagnostics.extend(page_diagnostics)

            if not page_reviews:
                if page == 1 and not reviews:
                    status_info["rss_status"] = "empty"
                else:
                    status_info["rss_status"] = "ok"

                break

            sources_used.add(source)

            new_reviews = 0

            for review in page_reviews:
                key = make_dedupe_key(review)

                if key in seen_reviews:
                    continue

                seen_reviews.add(key)
                reviews.append(review)
                new_reviews += 1

            # Защита от бесконечной пагинации.
            if new_reviews == 0:
                status_info["rss_status"] = "ok"
                break

        if reviews:
            status_info["rss_status"] = "ok"

    finally:
        session.close()

    status_info["sources_used"] = sorted(sources_used)

    df = pd.DataFrame(
        reviews,
        columns=REVIEW_COLUMNS,
    )

    if not df.empty:
        df["rating"] = pd.to_numeric(
            df["rating"],
            errors="coerce",
        ).astype("Int64")

    return df, app_info, status_info


# ============================================================
# STREAMLIT INTERFACE
# ============================================================

st.set_page_config(
    page_title="App Store Review Scraper",
    page_icon="⭐",
)

st.title("App Store Review Scraper")

st.write(
    "Сбор публично доступных отзывов "
    "из российского App Store."
)

app_input = st.text_input(
    "Введите ссылку App Store или ID приложения",
    placeholder=(
        "https://apps.apple.com/ru/app/"
        "example/id123456789"
    ),
)

if st.button("Собрать отзывы", type="primary"):

    try:
        if not app_input.strip():
            st.warning(
                "Введите ссылку App Store или ID приложения."
            )

        else:
            with st.spinner(
                "Проверяем приложение и загружаем отзывы..."
            ):
                df, app_info, status_info = scrape_reviews(
                    app_input
                )

            app_name = app_info.get(
                "trackName",
                "Неизвестное приложение",
            )

            app_id = app_info.get("trackId", "")

            st.subheader(app_name)
            st.write(f"App Store ID: **{app_id}**")
            st.write("Storefront: **ru (Россия)**")

            if status_info["rss_status"] == "empty":
                st.warning(
                    "Приложение найдено в российском App Store, "
                    "но публичный Apple Customer Reviews RSS сейчас "
                    "не вернул ни одного отзыва для storefront ru. "
                    "Это не доказывает, что отзывов у приложения нет: "
                    "публичный RSS Apple может отдавать пустую или "
                    "ограниченную выдачу."
                )

                with st.expander("Техническая информация"):
                    st.write(
                        "Проверены JSON- и XML-версии публичного RSS."
                    )

                    for item in status_info["diagnostics"]:
                        st.text(item)

            elif df.empty:
                st.warning(
                    "Apple RSS не вернул доступных отзывов."
                )

            else:
                st.success(
                    f"Собрано уникальных отзывов: {len(df)}"
                )

                if status_info["sources_used"]:
                    st.caption(
                        "Источник: "
                        + ", ".join(status_info["sources_used"])
                    )

                st.subheader("Предпросмотр данных")

                st.dataframe(
                    df,
                    use_container_width=True,
                )

                if df["rating"].notna().any():
                    st.subheader(
                        "Распределение рейтингов"
                    )

                    rating_counts = (
                        df["rating"]
                        .value_counts()
                        .sort_index()
                    )

                    st.bar_chart(rating_counts)

                csv_data = (
                    df.to_csv(index=False)
                    .encode("utf-8-sig")
                )

                st.download_button(
                    label="Скачать CSV",
                    data=csv_data,
                    file_name="app_store_reviews.csv",
                    mime="text/csv",
                )

    except InvalidAppInputError as exc:
        st.error(str(exc))

    except AppNotFoundError as exc:
        st.error(str(exc))

    except AppStoreRequestError as exc:
        st.error(
            "Не удалось получить отзывы через публичный "
            "интерфейс Apple. Возможно, RSS временно недоступен.\n\n"
            f"Техническая информация: {exc}"
        )

    except AppStoreResponseError as exc:
        st.error(
            "Apple вернул неожиданный ответ.\n\n"
            f"Техническая информация: {exc}"
        )

    except Exception as exc:
        st.error(
            f"Произошла непредвиденная ошибка: {exc}"
        )


st.divider()

st.caption(
    "Программа работает только с российским storefront (ru). "
    "Она собирает отзывы, которые доступны через публичный "
    "Apple Customer Reviews RSS. Публичный RSS ограничен "
    "и может не содержать всю историческую базу отзывов приложения."
)
