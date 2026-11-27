"""Thin credential lifecycle wrapper; inference stays in upstream CodexChatModel."""

import asyncio
import httpx
from langgraph.errors import GraphBubbleUp

from deerflow.models.credential_loader import CodexCliCredential
from deerflow.models.openai_codex_provider import CodexChatModel

from .auth import AuthStopped, auth_home, fresh_credential, read_credential


class CodexStopped(GraphBubbleUp):
    """Safe failure with no provider body, headers, token or automatic replay."""


class ManagedCodexChatModel(CodexChatModel):
    def _load_codex_auth(self):
        credential = read_credential(auth_home())
        return CodexCliCredential(credential.access_token, credential.account_id)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        # Keep one cancellation-aware implementation for both invocation APIs.
        return asyncio.run(self._agenerate(messages, stop=stop, run_manager=None, **kwargs))

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        try:
            async with asyncio.timeout(90):
                credential = await fresh_credential()
                if credential.account_id != self._account_id:
                    raise CodexStopped("codex_account_changed; recreate runtime after owner review")
                # Models can be cached/shared by DeerFlow. Never mutate their
                # auth fields across concurrent requests or account rotations.
                request_model = self.model_copy()
                request_model._access_token = credential.access_token
                request_model._account_id = credential.account_id
                request_model.retry_max_attempts = 1
                return await CodexChatModel._agenerate(
                    request_model, messages, stop=stop, run_manager=run_manager, **kwargs
                )
        except asyncio.CancelledError:
            raise
        except AuthStopped as error:
            raise CodexStopped(str(error)) from None
        except httpx.HTTPStatusError as error:
            raise CodexStopped(f"codex_http_{error.response.status_code}; no automatic replay") from None
        except TimeoutError:
            raise CodexStopped("codex_request_deadline; remote outcome unknown") from None
        except CodexStopped:
            raise
        except Exception:
            raise CodexStopped("codex_request_stopped; inspect auth readiness; no automatic replay") from None
