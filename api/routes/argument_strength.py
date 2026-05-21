from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from core.argument_strength import score_argument

router = APIRouter(prefix="/api/argument", tags=["argument"])


class ScoreRequest(BaseModel):
    argument_text: str
    metadata: dict = None


@router.post("/score")
async def score_endpoint(req: ScoreRequest):
    if not req.argument_text or not req.argument_text.strip():
        raise HTTPException(status_code=400, detail="argument_text is required")

    result = score_argument(req.argument_text, metadata=req.metadata or {})
    return result
