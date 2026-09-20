"""生成抖音 Argus 保护接口要求的 WebSign 参数."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from time import time
from urllib.parse import quote, unquote

WEB_SIGN_SALT = "A96D855A08C0A9707F8BEF0D9A527E4E"
WEB_SIGNATURE_PARAM = "x-secsdk-web-signature"


@dataclass(frozen=True, slots=True)
class SignedQuery:
    query: str
    signature: str
    timestamp: str


def _decode_pairs(query: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for part in query.split("&"):
        if not part:
            continue
        name, _, value = part.partition("=")
        pairs.append((unquote(name), unquote(value)))
    return pairs


def encode_pairs(pairs: list[tuple[str, str]]) -> str:
    """按网页 URLSearchParams 的字节形式序列化参数."""

    return "&".join(
        f"{quote(name, safe='*-._')}={quote(value, safe='*-._')}"
        for name, value in pairs
    )


def sign(query: str, uifid: str, *, timestamp: int | None = None) -> SignedQuery:
    """为包含 ``a_bogus`` 的最终 query 追加 WebSign."""

    stamp = str(int(time() if timestamp is None else timestamp))
    pairs = _decode_pairs(query)
    if not any(name == "uifid" for name, _ in pairs):
        pairs.append(("uifid", uifid))
    pairs.append(("timestamp", stamp))
    covered_query = encode_pairs(pairs)
    preimage = f"{uifid}_{stamp}_{WEB_SIGN_SALT}_{covered_query}"
    signature = hashlib.md5(preimage.encode()).hexdigest()
    return SignedQuery(
        query=f"{covered_query}&{WEB_SIGNATURE_PARAM}={signature}",
        signature=signature,
        timestamp=stamp,
    )
