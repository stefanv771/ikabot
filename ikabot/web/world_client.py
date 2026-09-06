# ikabot/web/world_client.py
import re
import json
import requests
from typing import Optional, Dict


class WorldClient:
    """Handles direct communication with a specific Ikariam game world server."""

    def __init__(self, serv_number: int, serv_lang: str):
        self.serv_number = serv_number
        self.serv_lang = serv_lang
        self.host = f"s{serv_number}-{serv_lang}.ikariam.gameforge.com"
        self.url_base = f"https://{self.host}/index.php?"
        
        self.session = requests.Session()
        self._setup_headers()

    def _setup_headers(self):
        self.session.headers.update({
            "Host": self.host,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": f"https://{self.host}/",
            "X-Requested-With": "XMLHttpRequest",
            "Connection": "keep-alive"
        })

    def set_session_cookie(self, cookie_value: str):
        """Sets the 'ikariam' session cookie manually or from login exchange."""
        cookie_obj = requests.cookies.create_cookie(
            domain=self.host,
            name="ikariam",
            value=cookie_value.strip()
        )
        self.session.cookies.set_cookie(cookie_obj)

    def login_via_url(self, login_url: str) -> bool:
        """
        Hits the Gameforge SSO redirect URL to automatically acquire world cookies.
        """
        resp = self.session.get(login_url, allow_redirects=True, timeout=30)
        # Verify that the 'ikariam' cookie was set in our session jar
        return "ikariam" in self.session.cookies

    def is_expired(self, text: str) -> bool:
        """Checks if the game session has expired."""
        return "index.php?logout" in text or '<a class="logout"' in text or not text

    def extract_action_request(self, text: str) -> Optional[str]:
        """Extracts the non-empty CSRF actionRequest token from the page."""
        # Find all occurrences of actionRequest: "token" or actionRequest="token" (32 hex characters)
        matches = re.findall(r'actionRequest["\']?\s*[:=]\s*["\']([a-f0-9]{32})["\']', text, re.IGNORECASE)
        if matches:
            return matches[0]
            
        # Fallback: find any non-empty alphanumeric token
        fallback_matches = [m for m in re.findall(r'actionRequest["\']?\s*[:=]\s*["\']([^"\']+)["\']', text, re.IGNORECASE) if m.strip()]
        return fallback_matches[0] if fallback_matches else None

    def extract_city_info(self, text: str) -> Dict[str, str]:
        """Extracts current city ID and name from the response text."""
        city_info = {"id": "Unknown", "name": "Unknown"}
        
        name_match = re.search(r'["\']cityName["\']\s*:\s*["\']([^"\']+)["\']', text)
        if name_match:
            city_info["name"] = name_match.group(1)
            
        id_match = re.search(r'["\']cityId["\']\s*:\s*["\']?(\d+)["\']?', text)
        if id_match:
            city_info["id"] = id_match.group(1)
            
        if city_info["name"] == "Unknown":
            alt_match = re.search(r'id=["\']js_cityBreadcrumb["\'][^>]*>([^<]+)', text)
            if alt_match:
                city_info["name"] = alt_match.group(1).strip()

        return city_info

    def get(self, query: str = "", params: Optional[Dict] = None) -> str:
        """Sends a GET request to the game server and returns the HTML/JSON text."""
        url = self.url_base + query
        resp = self.session.get(url, params=params or {}, timeout=30)
        
        if self.is_expired(resp.text):
            raise ConnectionResetError("Game session expired. Need to refresh cookies.")
            
        return resp.text

    def get_city_view(self) -> str:
        """Fetches the main city overview screen."""
        return self.get("view=city")