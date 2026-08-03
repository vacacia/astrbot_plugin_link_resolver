from __future__ import annotations

import atexit
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
SRC = ROOT / "src"
EXTENSIONS = ROOT / "extensions"

for site_packages in sorted((ROOT / ".venv" / "lib").glob("python*/site-packages")):
    text = str(site_packages)
    if site_packages.exists() and text not in sys.path:
        sys.path.insert(0, text)

for path in (ROOT, SRC, EXTENSIONS):
    text = str(path)
    if path.exists() and text not in sys.path:
        sys.path.insert(0, text)

from plugins.link_resolver.compat import (  # noqa: E402
    ensure_optional_dependency_stubs,
    install_astrbot_compat,
    set_runtime_state,
)

install_astrbot_compat()
ensure_optional_dependency_stubs()
set_runtime_state(
    data_dir=ROOT / ".pytest_cache" / "link_resolver_runtime_data",
    gateway=object(),
    plugin_config={},
)

try:
    from bilibili_api.utils import network as _bili_network

    _cleanup = getattr(_bili_network, "__clean", None)
    if _cleanup is not None:
        atexit.unregister(_cleanup)
except Exception:
    pass
