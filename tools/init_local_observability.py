"""Create local development credentials once; never print secret values."""

from pathlib import Path
import secrets

from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parents[1]
INFRA_ENV = ROOT / "infra" / "langfuse" / ".env"
APP_ENV = ROOT / ".env"


def main() -> None:
    if not INFRA_ENV.exists():
        values = {
            name: secrets.token_hex(32)
            for name in (
                "POSTGRES_PASSWORD", "CLICKHOUSE_PASSWORD", "REDIS_AUTH",
                "S3_SECRET_ACCESS_KEY", "SALT", "ENCRYPTION_KEY",
                "NEXTAUTH_SECRET", "ADMIN_PASSWORD",
            )
        }
        values["LANGFUSE_PUBLIC_KEY"] = "pk-lf-" + secrets.token_hex(16)
        values["LANGFUSE_SECRET_KEY"] = "sk-lf-" + secrets.token_hex(32)
        INFRA_ENV.parent.mkdir(parents=True, exist_ok=True)
        with INFRA_ENV.open("x", encoding="utf-8", newline="\n") as output:
            output.write("".join(f"{key}={value}\n" for key, value in values.items()))

    values = dotenv_values(INFRA_ENV)
    if not all(values.get(key) for key in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")):
        raise SystemExit("Infrastructure configuration is missing Langfuse keys.")
    if not APP_ENV.exists():
        with APP_ENV.open("x", encoding="utf-8", newline="\n") as output:
            output.write(
                "APP_NAME=aim_setupaiv2\nAPP_ENV=development\n"
                "LLM_PROVIDER=openai\nLLM_MODEL=gpt-6-luna\n"
                "OPENAI_API_KEY=\nDEEPSEEK_API_KEY=\n"
                "LANGSMITH_TRACING=false\n"
                "LANGFUSE_BASE_URL=http://localhost:3000\n"
                f"LANGFUSE_PUBLIC_KEY={values['LANGFUSE_PUBLIC_KEY']}\n"
                f"LANGFUSE_SECRET_KEY={values['LANGFUSE_SECRET_KEY']}\n"
            )
    print("Local configuration ready. Existing files preserved; no secrets displayed.")
    print("Admin: admin@aim-setupaiv2.local; password: ADMIN_PASSWORD in infra/langfuse/.env")
    print("Set OPENAI_API_KEY locally in the root .env before a live model check.")


if __name__ == "__main__":
    main()
