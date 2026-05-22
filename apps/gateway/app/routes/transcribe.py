"""Voice-to-text endpoint."""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import time

import httpx
from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel

log = logging.getLogger(__name__)
router = APIRouter(tags=["transcribe"])


_WHISPER_VERSION = (
    "8099696689d249cf8b122d833c36ac3f75505c666a395ca40ef26f68e7d3d16e"
)
_REPLICATE_API = "https://api.replicate.com/v1/predictions"
_POLL_INTERVAL_S = 1.0
_TIMEOUT_S = 60.0


class TranscribeResponse(BaseModel):
    transcript: str
    duration_ms: int
    detected_language: str | None = None


@router.post("/transcribe", response_model=TranscribeResponse)
async def transcribe(audio: UploadFile = File(...)) -> TranscribeResponse:
    token = os.environ.get("REPLICATE_API_TOKEN")
    if not token:
        raise HTTPException(500, detail="REPLICATE_API_TOKEN is not configured")

    data = await audio.read()
    if not data:
        raise HTTPException(400, detail="empty audio payload")

    mime = audio.content_type or "audio/webm"
    data_uri = f"data:{mime};base64,{base64.b64encode(data).decode()}"

    started = time.perf_counter()
    headers = {"Authorization": f"Token {token}", "Content-Type": "application/json"}
    payload = {
        "version": _WHISPER_VERSION,
        "input": {"audio": data_uri, "model": "large-v3", "translate": False},
    }

    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        create = await client.post(_REPLICATE_API, headers=headers, json=payload)
        if create.status_code >= 400:
            raise HTTPException(502, detail=f"replicate create failed: {create.text[:200]}")
        pred = create.json()
        get_url = pred["urls"]["get"]

        deadline = time.perf_counter() + _TIMEOUT_S
        while pred["status"] in ("starting", "processing"):
            if time.perf_counter() > deadline:
                raise HTTPException(504, detail="transcription timed out")
            await asyncio.sleep(_POLL_INTERVAL_S)
            poll = await client.get(get_url, headers=headers)
            pred = poll.json()

    if pred["status"] != "succeeded":
        raise HTTPException(502, detail=pred.get("error") or f"status={pred.get('status')}")

    output = pred.get("output") or {}
    transcript = (output.get("transcription") or "").strip() if isinstance(output, dict) else str(output)
    if not transcript:
        raise HTTPException(502, detail="empty transcription returned by whisper")

    dur_ms = int((time.perf_counter() - started) * 1000)
    log.info("transcribe: %d chars in %dms", len(transcript), dur_ms)
    return TranscribeResponse(
        transcript=transcript,
        duration_ms=dur_ms,
        detected_language=(output.get("detected_language") if isinstance(output, dict) else None),
    )
