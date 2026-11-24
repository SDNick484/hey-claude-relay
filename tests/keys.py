"""A throwaway signing key and a way to mint Cloudflare-Access-shaped assertions with it."""

import time
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

TEAM = "example.cloudflareaccess.com"
AUD = "aud-1"
CALLER = "phone.access"  # a service token's client ID, as Access puts it in common_name

PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


async def resolver(token: str) -> Any:
    """The key resolver tests give the verifier: always the test key, no network."""
    return PRIVATE_KEY.public_key()


def mint(
    *,
    aud: str = AUD,
    iss: str = f"https://{TEAM}",
    common_name: Any = CALLER,
    email: str | None = None,
    expires_in: int = 300,
    key: Any = PRIVATE_KEY,
    alg: str = "RS256",
    kid: str | None = None,
    drop: tuple[str, ...] = (),
) -> str:
    """An assertion as Access signs one for a service token (no email) or a person (an email)."""
    now = int(time.time())
    claims = {"iss": iss, "aud": [aud], "iat": now, "exp": now + expires_in, "common_name": common_name, "email": email}
    claims = {k: v for k, v in claims.items() if v is not None and k not in drop}
    return jwt.encode(claims, key, algorithm=alg, headers={"kid": kid} if kid else None)
