import io
import random
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.language_detect import detect_language
from app.tts_engine import synthesize, warm_up
from app.voices import pick_voice


@asynccontextmanager
async def lifespan(app: FastAPI):
    warm_up()
    yield


app = FastAPI(lifespan=lifespan)


class TtsRequest(BaseModel):
    text: str
    voice_gender: str | None = None


@app.post("/tts")
def tts(req: TtsRequest):
    text = req.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="text must not be empty")

    language = detect_language(text)
    gender = req.voice_gender if req.voice_gender in ("male", "female") else random.choice(["male", "female"])
    voice_id = pick_voice(language, gender)

    try:
        audio_bytes = synthesize(text, voice_id, language)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return StreamingResponse(io.BytesIO(audio_bytes), media_type="audio/wav")
