import streamlit as st

from scraper import (
    AppNotFoundError,
    AppStoreRequestError,
    AppStoreResponseError,
    InvalidAppInputError,
    dataframe_to_csv_bytes,
    extract_app_id,
    scrape_reviews,
)


st.set_page_config(page_title="App Store Review Scraper", page_icon="⭐")

st.title("App Store Review Scraper")
st.write("Сбор публично доступных отзывов из российского App Store.")

app_input = st.text_input(
    "Введите ссылку App Store или ID приложения",
    placeholder="https://apps.apple.com/ru/app/.../id123456789",
)

if st.button("Собрать отзывы", type="primary"):
    try:
        app_id = extract_app_id(app_input)

        with st.spinner("Получаем отзывы из App Store..."):
            df = scrape_reviews(app_id, country="ru")

        if df.empty:
            st.warning(
                f"Приложение с ID {app_id} найдено, но публичный RSS App Store "
                "не вернул отзывов для storefront 'ru'."
            )
        else:
            st.success(f"Собрано отзывов: {len(df)}")
            st.dataframe(df, use_container_width=True)

            st.download_button(
                label="Скачать CSV",
                data=dataframe_to_csv_bytes(df),
                file_name="app_store_reviews.csv",
                mime="text/csv",
            )

    except InvalidAppInputError as exc:
        st.error(str(exc))
    except AppNotFoundError as exc:
        st.error(str(exc))
    except AppStoreRequestError as exc:
        st.error(f"Не удалось получить данные от App Store. {exc}")
    except AppStoreResponseError as exc:
        st.error(f"App Store вернул неожиданный ответ. {exc}")
    except Exception as exc:
        st.error(f"Непредвиденная ошибка: {exc}")
