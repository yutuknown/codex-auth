import json
import os
import pytest
from codex_auth.auth_writer import write_auth_json

def test_write_auth_json(tmp_path):
    # Setup test token data
    tokens = {
        "access_token": "test_access",
        "refresh_token": "test_refresh",
        "account_id": "test_acc_id"
    }
    
    output_file = tmp_path / "auth.json"
    
    # Write to temp file
    write_auth_json(tokens, path=str(output_file))
    
    # Verify file was created
    assert output_file.exists()
    
    # Verify contents
    with open(output_file, "r") as f:
        data = json.load(f)
        
    assert data["auth_mode"] == "chatgpt"
    assert data["OPENAI_API_KEY"] is None
    assert data["tokens"]["access_token"] == "test_access"
    assert data["tokens"]["refresh_token"] == "test_refresh"
    assert data["tokens"]["account_id"] == "test_acc_id"
    assert data["tokens"]["id"] == "test_acc_id"
    assert "last_refresh" in data
