"""A stand-in provider, so the load suite can run without spending money.

The absolute numbers a run produces against this are meaningless — it answers in
milliseconds, where a real model takes seconds — and that is fine, because what CI checks
is the *change* between commits. An accidental per-request database round trip shows up
against this mock exactly as clearly as it would against OpenAI, and rather more clearly,
since the provider's own variance is not there to hide in.

It is also the only honest way to run the suite on a pull request: a load test that calls a
real provider is a load test with a bill and a rate limit attached.

    uv run python deploy/loadtest/mock_upstream.py --port 9099 --delay-ms 40

Speaks enough of the OpenAI API for the gateway's adapter: ``/v1/chat/completions``,
streaming and not, with a usage block, because the rate limiter settles against it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

#: The answer, as tokens. Fixed, because a load test whose responses vary in length is
#: measuring response length.
ANSWER = (
    "The refund window is thirty days from the start of the billing period, "
    "and a partial month is prorated to the day."
)
WORDS = ANSWER.split(" ")


def build_app(delay_ms: float, ttft_ms: float) -> FastAPI:
    app = FastAPI(title="mock upstream", docs_url=None, openapi_url=None)

    def usage(prompt: str) -> dict[str, int]:
        # Roughly four characters per token, which is what the gateway's own estimate
        # assumes — so the limiter's settle step is exercised with plausible numbers.
        prompt_tokens = max(1, len(prompt) // 4)
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": len(WORDS),
            "total_tokens": prompt_tokens + len(WORDS),
        }

    @app.post("/v1/chat/completions")
    async def completions(request: Request) -> Any:
        body = await request.json()
        prompt = json.dumps(body.get("messages", []))
        model = body.get("model", "mock")
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())

        if not body.get("stream"):
            await asyncio.sleep(delay_ms / 1000)
            return JSONResponse(
                {
                    "id": completion_id,
                    "object": "chat.completion",
                    "created": created,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": " ".join(WORDS)},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": usage(prompt),
                }
            )

        async def frames() -> AsyncIterator[str]:
            # A gap before the first frame and between the rest, so time-to-first-byte and
            # inter-frame latency are separable in the results — the gateway's own overhead
            # lands in the first of those and nowhere else.
            await asyncio.sleep(ttft_ms / 1000)
            for index, word in enumerate(WORDS):
                chunk = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": ("" if index == 0 else " ") + word},
                            "finish_reason": None,
                        }
                    ],
                }
                yield f"data: {json.dumps(chunk)}\n\n"
                await asyncio.sleep(delay_ms / 1000 / max(1, len(WORDS)))
            final = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": usage(prompt),
            }
            yield f"data: {json.dumps(final)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(frames(), media_type="text/event-stream")

    @app.get("/v1/models")
    async def models() -> dict[str, Any]:
        return {"object": "list", "data": [{"id": "mock", "object": "model"}]}

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9099)
    parser.add_argument(
        "--delay-ms",
        type=float,
        default=40.0,
        help="how long a whole completion takes (default 40)",
    )
    parser.add_argument(
        "--ttft-ms",
        type=float,
        default=20.0,
        help="delay before the first streamed frame (default 20)",
    )
    args = parser.parse_args()
    uvicorn.run(
        build_app(args.delay_ms, args.ttft_ms),
        host=args.host,
        port=args.port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
