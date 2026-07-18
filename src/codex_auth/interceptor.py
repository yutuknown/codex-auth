captured = {}

def on_response(response):
    """
    Intercepts the response and captures the token and account ID
    from the https://chatgpt.com/api/auth/session endpoint.
    """
    if "https://chatgpt.com/api/auth/session" in response.url:
        try:
            # Depending on the HTTP client (Playwright sync, requests, etc.),
            # .json() is a method that returns the parsed JSON body.
            data = response.json()
            
            if data and isinstance(data, dict):
                captured["access_token"] = data.get("accessToken")
                user = data.get("user", {})
                captured["account_id"] = user.get("id")
        except Exception:
            pass
