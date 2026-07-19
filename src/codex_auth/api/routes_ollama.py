import time

from fastapi import APIRouter, HTTPException, Request

from ..providers.openai.provider import provider

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
    
    reset_chat = data.get("reset_chat", False)
    if reset_chat:
        await provider.reset_session(requested_model)
        
    prompt = ""
    images = []
    
    if messages:
        last_msg = messages[-1]
        prompt = last_msg.get("content", "")
        if "images" in last_msg and isinstance(last_msg["images"], list):
            images.extend(last_msg["images"])
    
    try:
        web_search = data.get("web_search", False)
        
        full_response = ""
        async for chunk in provider.generate_stream(prompt.strip(), files=images, web_search=web_search):
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
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
