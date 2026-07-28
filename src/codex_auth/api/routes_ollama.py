import time

from fastapi import APIRouter, HTTPException, Request

from ..providers.openai.provider import ProviderBusyError, provider

router = APIRouter()

@router.get("/api/tags")
async def ollama_tags():
    real_models = await provider.fetch_models()
    models_data = []
    for m in real_models:
        slug = m.get("slug", "auto")
        tags = m.get("tags", [])
        families = ["gpt"]
        
        product_features = m.get("product_features", {})
        attachments = product_features.get("attachments", {})
        has_image_support = "image_mime_types" in attachments and len(attachments["image_mime_types"]) > 0
        
        if "vision" in tags or "multimodal" in tags or "gpt4" in tags or has_image_support:
            families.append("clip")
            
        models_data.append({
            "name": slug,
            "model": slug,
            "modified_at": time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime()),
            "size": 4700000000,
            "digest": "stealth-proxy",
            "details": {
                "parent_model": "",
                "format": "gguf",
                "family": "gpt",
                "families": families,
                "parameter_size": "unknown",
                "quantization_level": "none"
            }
        })
    return {
        "models": models_data
    }

@router.post("/api/show")
async def ollama_show(request: Request):
    data = await request.json()
    model_name = data.get("name", "gpt-4o")
    
    real_models = await provider.fetch_models()
    model_info = next((m for m in real_models if m.get("slug") == model_name), {})
    
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
        web_search = data.get("web_search", False)
        
        full_response = ""
        async for chunk in provider.generate_stream(
            prompt.strip(),
            files=images,
            web_search=web_search,
            model=requested_model,
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
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
