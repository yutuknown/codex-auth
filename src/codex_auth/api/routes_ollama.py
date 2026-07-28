import time

from fastapi import APIRouter, HTTPException, Request

from ..providers.errors import ProviderBusyError, ProviderError
from ..providers.runtime import registry

router = APIRouter()


@router.get("/api/tags")
async def ollama_tags():
    models_data = []
    for provider_id in registry.ids():
        provider = registry.get(provider_id)
        if not provider.is_configured():
            continue
        await registry.ensure_initialized(provider_id)
        real_models = await provider.fetch_models()
        for m in real_models:
            slug = m.get("slug", "auto")
            models_data.append(_ollama_model(provider_id, slug, m))
            if provider_id == registry.default_provider_id:
                models_data.append(_ollama_model(provider_id, slug, m, alias=True))
    return {"models": models_data}


def _ollama_model(provider_id: str, slug: str, m: dict, alias: bool = False) -> dict:
    tags = m.get("tags", [])
    family = "gpt" if provider_id == "openai-web" else provider_id
    families = [family]

    product_features = m.get("product_features", {})
    attachments = product_features.get("attachments", {})
    has_image_support = bool(attachments.get("image_mime_types"))

    if "vision" in tags or "multimodal" in tags or "gpt4" in tags or has_image_support:
        families.append("clip")

    model_id = slug if alias else f"{provider_id}:{slug}"
    return {
        "name": model_id,
        "model": model_id,
        "modified_at": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        "size": 4700000000,
        "digest": f"codex-auth-{provider_id}",
        "details": {
            "parent_model": "",
            "format": "gguf",
            "family": family,
            "families": families,
            "parameter_size": "unknown",
            "quantization_level": "none",
        },
    }


@router.post("/api/show")
async def ollama_show(request: Request):
    data = await request.json()
    model_name = data.get("name", "gpt-4o")
    
    try:
        selection = registry.select(model_name)
        if selection.provider_id != registry.default_provider_id:
            await registry.ensure_initialized(selection.provider_id)
    except ProviderError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"message": str(exc), "type": exc.error_type},
        ) from exc
    real_models = await selection.provider.fetch_models()
    model_info = next((m for m in real_models if m.get("slug") == selection.model), {})
    
    tags = model_info.get("tags", [])
    families = ["gpt"]
    
    product_features = model_info.get("product_features", {})
    attachments = product_features.get("attachments", {})
    has_image_support = "image_mime_types" in attachments and len(attachments["image_mime_types"]) > 0
    
    if "vision" in tags or "multimodal" in tags or "gpt4" in tags or has_image_support:
        families.append("clip")
        
    return {
        "license": "OpenAI",
        "modelfile": f"FROM {model_name}",
        "parameters": f"num_ctx {model_info.get('max_tokens', 32768)}",
        "template": "{{ .Prompt }}",
        "details": {
            "parent_model": "",
            "format": "gguf",
            "family": "gpt",
            "families": families,
            "parameter_size": "unknown",
            "quantization_level": "none"
        }
    }

@router.get("/api/version")
async def ollama_version():
    return {"version": "0.1.43"}

@router.post("/api/chat")
async def ollama_chat(request: Request):
    data = await request.json()
    messages = data.get("messages", [])
    requested_model = data.get("model", "auto")
    explicit_provider = data.get("provider")
    if data.get("tools"):
        raise HTTPException(status_code=501, detail="Ollama function tools are not implemented by this proxy")

    transcript = []
    images = []
    for message in messages:
        content = str(message.get("content") or "").strip()
        if content:
            transcript.append((str(message.get("role") or "user"), content))
        if isinstance(message.get("images"), list):
            images.extend(message["images"])
    if len(transcript) <= 1:
        prompt = transcript[0][1] if transcript else ""
    else:
        prompt = "Use this transcript as context and answer the final user message.\n\n" + "\n\n".join(
            f"{role.upper()}:\n{content}" for role, content in transcript
        )
    
    try:
        selection = registry.select(requested_model, explicit_provider)
        if selection.provider_id != registry.default_provider_id:
            await registry.ensure_initialized(selection.provider_id)
        web_search = data.get("web_search", False)
        
        full_response = ""
        async for chunk in selection.provider.generate_stream(
            prompt.strip(),
            files=images,
            web_search=web_search,
            model=selection.model,
            realtime=False,
        ):
            full_response += chunk
            
        return {
            "model": data.get("model", "llama3"),
            "created_at": time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime()),
            "message": {
                "role": "assistant",
                "content": full_response
            },
            "done": True
        }
    except ProviderBusyError as e:
        raise HTTPException(status_code=429, detail=str(e), headers={"Retry-After": "5"})
    except ProviderError as e:
        raise HTTPException(
            status_code=e.status_code,
            detail={"message": str(e), "type": e.error_type},
        ) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
