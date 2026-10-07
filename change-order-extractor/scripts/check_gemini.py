"""Quick check that the Gemini API key and model work."""

from google import genai

from change_order_extractor.config import get_settings


def main() -> None:
    settings = get_settings()
    if settings.gemini_api_key is None:
        raise SystemExit("GEMINI_API_KEY is not set. Copy .env.example to .env and add your key.")

    client = genai.Client(api_key=settings.gemini_api_key.get_secret_value())
    response = client.models.generate_content(
        model=settings.gemini_model,
        contents="Reply with exactly: Gemini connection OK",
    )
    print(f"Model: {settings.gemini_model}")
    print(f"Reply: {response.text}")


if __name__ == "__main__":
    main()
