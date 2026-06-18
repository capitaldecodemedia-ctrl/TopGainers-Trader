"""
Fyers Auth — Step 2: Exchange auth_code for access_token.

Run after Step 1, passing the full redirected URL as an argument:
    python auth/step2_get_token.py "https://trade.fyers.in/api-login/redirect-uri/index.html?auth_code=XXXX&state=..."

Writes FYERS_ACCESS_TOKEN into .env automatically.
"""

import re
import sys
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).parent.parent))

from fyers_apiv3 import fyersModel
from auth.env_utils import load_env_value, update_env_value


def extract_auth_code(redirect_url: str) -> str:
    parsed = urlparse(redirect_url)
    qs = parse_qs(parsed.query)
    if "auth_code" in qs:
        return qs["auth_code"][0]
    match = re.search(r"auth_code=([^&]+)", redirect_url)
    if match:
        return match.group(1)
    raise ValueError("Could not find auth_code in the pasted URL")


def main():
    if len(sys.argv) < 2:
        print('USAGE: python auth/step2_get_token.py "FULL_REDIRECT_URL"')
        sys.exit(1)

    redirect_url = sys.argv[1]
    auth_code = extract_auth_code(redirect_url)
    print(f"✓ Auth code extracted: {auth_code[:20]}...")

    app_id       = load_env_value("FYERS_APP_ID")
    secret_id    = load_env_value("FYERS_SECRET_ID")
    redirect_uri = load_env_value("FYERS_REDIRECT_URI")

    session = fyersModel.SessionModel(
        client_id=app_id,
        secret_key=secret_id,
        redirect_uri=redirect_uri,
        response_type="code",
        grant_type="authorization_code"
    )
    session.set_token(auth_code)
    response = session.generate_token()

    if response.get("s") != "ok":
        print(f"ERROR: Token exchange failed: {response}")
        sys.exit(1)

    access_token = response["access_token"]
    update_env_value("FYERS_ACCESS_TOKEN", access_token)

    print("\n✓ SUCCESS — access token saved to .env")
    print(f"  Token (truncated): {access_token[:25]}...")
    print("\nValid until ~6 AM IST tomorrow. Re-run step1+step2 each trading morning.")


if __name__ == "__main__":
    main()
