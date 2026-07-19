import datetime
import json
from pathlib import Path


def write_auth_json(tokens: dict, path: str) -> None:
    """
    Writes the provided authentication tokens to a JSON file at the specified path,
    formatting it for Codex compatibility.
    """
    tokens_copy = dict(tokens)
    if "account_id" in tokens_copy and "id" not in tokens_copy:
        tokens_copy["id"] = tokens_copy["account_id"]
        
    data = {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": tokens_copy,
        "last_refresh": datetime.datetime.now(datetime.timezone.utc).isoformat()
    }
    
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)
