from typing import Any


def parse_netscape_cookies(text: str) -> list[dict[str, Any]]:
    """Parse the seven-column Netscape cookie format without writing a temp file."""
    cookies = []
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_") :]
        fields = line.split("\t")
        if len(fields) != 7:
            raise ValueError(f"Invalid Netscape cookie record on line {line_number}")
        domain, _, path, secure, expires, name, value = fields
        cookies.append(
            {
                "name": name,
                "value": value,
                "domain": domain,
                "path": path or "/",
                "secure": secure.upper() == "TRUE",
                "expires_at": int(expires) if expires.isdigit() and int(expires) > 0 else None,
            }
        )
    if not cookies:
        raise ValueError("The cookie source contains no Netscape cookie records")
    return cookies
