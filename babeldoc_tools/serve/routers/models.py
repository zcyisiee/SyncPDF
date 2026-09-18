"""Saved models; saving is offline, testing is an explicit potentially paid request."""
from fastapi import APIRouter
from fastapi import Request
from fastapi import Response
from pydantic import ValidationError

from babeldoc_tools.common import ToolError
from babeldoc_tools.serve.models import ModelUpdate
from babeldoc_tools.serve.models import call_model
from babeldoc_tools.serve.models import delete_model
from babeldoc_tools.serve.models import load_models
from babeldoc_tools.serve.models import metadata
from babeldoc_tools.serve.models import require_model
from babeldoc_tools.serve.models import save_model
from babeldoc_tools.serve.schemas import API_PREFIX


def models_router(store):
    router = APIRouter(prefix=API_PREFIX, tags=["models"])

    @router.get("/models")
    def list_models():
        return [metadata(entry) for _, entry in sorted(load_models(store.store_base).items())]

    @router.put("/models")
    async def put_model(request: Request):
        # Do not let Pydantic/FastAPI echo credential-bearing inputs in error detail.
        try:
            update = ModelUpdate.model_validate(await request.json())
        except (ValidationError, ValueError, UnicodeDecodeError):
            raise ToolError("model_invalid", "Invalid model configuration; check ID, label, model, Base URL and API key") from None
        return save_model(store.store_base, update)

    @router.get("/models/{model_id}")
    def get_model(model_id: str):
        return metadata(require_model(store.store_base, model_id))

    @router.delete("/models/{model_id}", status_code=204)
    def remove_model(model_id: str):
        delete_model(store.store_base, model_id)
        return Response(status_code=204)

    @router.post("/models/{model_id}/test")
    def test_model(model_id: str):
        """Send a short generation request. May incur provider charges."""
        call_model(store.store_base, model_id, "Reply only OK.", test=True)
        return {"ok": True}

    return router
