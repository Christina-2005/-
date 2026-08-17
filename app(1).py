import re
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
            "User-Agent": "Mozilla/5.0 AppStoreReviewScraper/1.0",
            "Accept": "application/json,text/plain,*/*",
        }
    )

    return session


def request_json(session, url, params=None):
    try:
        response = session.get(
            url,
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()

    except requests.RequestException as exc:
        raise AppStoreRequestError(
            f"Ошибка запроса к App Store: {exc}"
        ) from exc

    try:
        data = response.json()

    except ValueError as exc:
        raise AppStoreResponseError(
            "App Store вернул некорректный JSON."
        ) from exc

    if not isinstance(data, dict):
        raise AppStoreResponseError(
            "App Store вернул ответ неожиданного формата."
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


def parse_review_entry(entry, app_id):
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

    try:
        app_info = validate_app_exists(app_id, session)

        for page in range(1, MAX_PAGES + 1):
            url = (
                f"https://itunes.apple.com/"
                f"{COUNTRY}/rss/customerreviews/"
                f"page={page}/"
                f"id={app_id}/"
                f"sortby=mostrecent/json"
            )

            data = request_json(session, url)

            feed = data.get("feed")

            if not isinstance(feed, dict):
                raise AppStoreResponseError(
                    "App Store вернул неожиданную структуру данных."
                )

            entries = feed.get("entry", [])

            if entries is None:
                entries = []

            if isinstance(entries, dict):
                entries = [entries]

            if not isinstance(entries, list):
                raise AppStoreResponseError(
                    "Поле отзывов имеет неожиданный формат."
                )

            if not entries:
                break

            page_reviews = []

            for entry in entries:
                review = parse_review_entry(entry, app_id)

                if review is not None:
                    page_reviews.append(review)

            if not page_reviews:
                break

            new_reviews = 0

            for review in page_reviews:
                key = make_dedupe_key(review)

                if key in seen_reviews:
                    continue

                seen_reviews.add(key)
                reviews.append(review)
                new_reviews += 1

            if new_reviews == 0:
                break

    finally:
        session.close()

    df = pd.DataFrame(reviews, columns=REVIEW_COLUMNS)

    if not df.empty:
        df["rating"] = pd.to_numeric(
            df["rating"],
            errors="coerce"
        ).astype("Int64")

    return df, app_info


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
            with st.spinner("Загружаем отзывы..."):
                df, app_info = scrape_reviews(app_input)

            app_name = app_info.get(
                "trackName",
                "Неизвестное приложение"
            )

            app_id = app_info.get("trackId", "")

            st.subheader(app_name)
            st.write(f"App Store ID: **{app_id}**")

            if df.empty:
                st.warning(
                    "Приложение найдено, но публично доступные "
                    "отзывы для российского App Store отсутствуют."
                )
            else:
                st.success(f"Собрано отзывов: {len(df)}")

                st.subheader("Предпросмотр данных")

                st.dataframe(
                    df,
                    use_container_width=True,
                )

                if df["rating"].notna().any():
                    st.subheader("Распределение рейтингов")

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
            "Ошибка соединения с App Store.\n\n"
            + str(exc)
        )

    except AppStoreResponseError as exc:
        st.error(
            "App Store вернул неожиданный ответ.\n\n"
            + str(exc)
        )

    except Exception as exc:
        st.error(
            f"Произошла непредвиденная ошибка: {exc}"
        )


st.divider()

st.caption(
    "Программа получает все отзывы, доступные через "
    "публичный Apple Customer Reviews RSS интерфейс "
    "для российского storefront (ru). Apple ограничивает "
    "количество отзывов, доступных через этот интерфейс, "
    "поэтому результат может не содержать всю историческую "
    "базу отзывов приложения."
)
