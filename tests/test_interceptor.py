from unittest.mock import Mock

from codex_auth.interceptor import captured, on_response


def test_on_response_success():
    # Setup mock response
    mock_response = Mock()
    mock_response.url = "https://chatgpt.com/api/auth/session"
    mock_response.json.return_value = {
        "accessToken": "mocked_access_token",
        "user": {
            "id": "mocked_user_id"
        }
    }
    
    # Call interceptor
    on_response(mock_response)
    
    # Verify captured
    assert captured.get("access_token") == "mocked_access_token"
    assert captured.get("account_id") == "mocked_user_id"

def test_on_response_ignore_other_urls():
    # Setup mock response for unrelated URL
    mock_response = Mock()
    mock_response.url = "https://chatgpt.com/api/other"
    mock_response.json.return_value = {
        "accessToken": "should_not_capture"
    }
    
    # Clear captured
    captured.clear()
    
    # Call interceptor
    on_response(mock_response)
    
    # Verify not captured
    assert "access_token" not in captured
