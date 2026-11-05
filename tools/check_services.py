"""Check local trace persistence; --live additionally makes one paid model call."""

import argparse
import json
import logging
import time
from typing import TypedDict

from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_openai import ChatOpenAI
from langfuse import Langfuse
from langfuse.langchain import CallbackHandler
from langgraph.graph import END, START, StateGraph
from langsmith import tracing_context

from service_settings import Settings


class State(TypedDict):
    prompt: str
    answer: str


class CheckFailure(RuntimeError):
    """A diagnostic containing only messages authored by this check."""


def run_check(settings: Settings, *, live: bool) -> str:
    if live:
        endpoint, key = settings.model_credentials()
        model = ChatOpenAI(
            model=settings.llm_model, api_key=key, base_url=endpoint,
            use_responses_api=True, timeout=30, max_retries=0,
            max_tokens=256, store=False,
            **({"reasoning_effort": "none"} if settings.llm_provider == "openai" else {}),
        )
    else:
        model = FakeListChatModel(responses=["LOCAL_TRACE_OK"])

    client = Langfuse(
        public_key=settings.langfuse_public_key.get_secret_value(),
        secret_key=settings.langfuse_secret_key.get_secret_value(),
        base_url=settings.langfuse_base_url, environment=settings.app_env,
        timeout=10, flush_at=1,
    )
    try:
        if not client.auth_check():
            raise CheckFailure("Local Langfuse authentication failed")
        trace_id = client.create_trace_id()
        handler = CallbackHandler(
            public_key=settings.langfuse_public_key.get_secret_value(),
            trace_context={"trace_id": trace_id},
        )

        def respond(state: State) -> dict[str, str]:
            answer = model.invoke(state["prompt"]).text
            if not answer.strip():
                raise CheckFailure("Model returned no text")
            return {"answer": answer}

        builder = StateGraph(State)
        builder.add_node("respond", respond)
        builder.add_edge(START, "respond")
        builder.add_edge("respond", END)
        # No LangSmith Cloud traffic, even if enabled in the parent shell.
        with tracing_context(enabled=False):
            result = builder.compile().invoke(
                {"prompt": "Reply with exactly LOCAL_TRACE_OK.", "answer": ""},
                config={
                    "callbacks": [handler],
                    "run_name": f"{settings.app_name}-{'live' if live else 'local'}-check",
                    "metadata": {"app_name": settings.app_name, "mode": "live" if live else "fake"},
                },
            )
        client.flush()
        # Ingestion is asynchronous: poll only this trace, with a fixed deadline.
        deadline = time.monotonic() + 45
        while True:
            observations = client.api.observations.get_many(
                trace_id=trace_id, environment=settings.app_env,
                fields="core,basic,io,model", limit=100,
            ).data
            result_persisted = any(
                item.is_root_observation and item.output
                and json.loads(item.output) == result
                for item in observations
            )
            if result_persisted and any(item.type == "GENERATION" for item in observations):
                break
            if time.monotonic() >= deadline:
                raise CheckFailure(f"Trace {trace_id} persistence did not complete within 45 seconds")
            time.sleep(2)
        url = client.get_trace_url(trace_id=trace_id)
        print(f"PASS: {'real model' if live else 'fake model (no API cost)'}, graph and persisted trace")
        print(f"Environment: {settings.app_env}; trace: {url}")
        return trace_id
    finally:
        client.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Make one real model request")
    args = parser.parse_args()
    # SDK errors may contain request payloads; report a safe summary instead.
    logging.disable(logging.CRITICAL)
    try:
        settings = Settings()
        if args.live:
            settings.model_credentials()
    except ValueError as error:
        raise SystemExit(f"Configuration error: {error}") from None
    try:
        run_check(settings, live=args.live)
    except CheckFailure as error:
        raise SystemExit(f"FAIL: {error}") from None
    except Exception as error:
        status = getattr(error, "status_code", None)
        raise SystemExit(
            f"FAIL: {type(error).__name__}"
            + (f" (HTTP {status})" if status else "")
            + ". Check local service health, credentials and model access. No secrets printed."
        ) from None


if __name__ == "__main__":
    main()
