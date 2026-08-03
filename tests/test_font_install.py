# ruff: noqa: E402
"""Tests for managed font installation and config wiring.

Run from the AcaBot repo root:
    .venv/bin/python -m pytest extensions/plugins/link_resolver/tests/test_font_install.py -q
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from plugins.link_resolver.core.common.card_renderer.utils import (
    find_default_font,
    find_emoji_font,
)
from plugins.link_resolver.core.common.font_manager import (
    ManagedFontPaths,
    install_managed_fonts,
)
from plugins.link_resolver.main import LinkResolver


class FakeResponse:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self):
        return None

    def iter_bytes(self):
        yield self.payload


class TestFontInstall(unittest.TestCase):
    def tearDown(self):
        from plugins.link_resolver.core.common.font_manager import (
            set_user_font_paths,
        )

        set_user_font_paths(None, None)

    def test_configure_managed_fonts_disabled_skips_install(self):
        plugin = LinkResolver.__new__(LinkResolver)
        plugin.config = {"general_settings": {"auto_install_fonts": False}}
        plugin._get_config_value = LinkResolver._get_config_value.__get__(
            plugin, LinkResolver
        )

        with (
            patch(
                "plugins.link_resolver.main.set_managed_fonts_enabled"
            ) as set_enabled,
            patch(
                "plugins.link_resolver.main.install_managed_fonts"
            ) as install_mock,
        ):
            LinkResolver._configure_managed_fonts(plugin)

        set_enabled.assert_called_once_with(False)
        install_mock.assert_not_called()
        self.assertFalse(plugin.font_auto_install_enabled)

    def test_configure_managed_fonts_enabled_installs(self):
        plugin = LinkResolver.__new__(LinkResolver)
        plugin.config = {"general_settings": {"auto_install_fonts": True}}
        plugin._get_config_value = LinkResolver._get_config_value.__get__(
            plugin, LinkResolver
        )
        expected = ManagedFontPaths(
            primary=Path("/tmp/NotoSansCJKsc-Regular.otf"),
            emoji=Path("/tmp/OpenMoji-black-glyf.ttf"),
        )

        with (
            patch(
                "plugins.link_resolver.main.set_managed_fonts_enabled"
            ) as set_enabled,
            patch(
                "plugins.link_resolver.main.install_managed_fonts",
                return_value=expected,
            ) as install_mock,
        ):
            LinkResolver._configure_managed_fonts(plugin)

        set_enabled.assert_called_once_with(True)
        install_mock.assert_called_once()
        self.assertTrue(plugin.font_auto_install_enabled)
        self.assertTrue(plugin.managed_primary_font_ready)
        self.assertTrue(plugin.managed_emoji_font_ready)

    def test_configure_managed_fonts_sets_custom_paths(self):
        plugin = LinkResolver.__new__(LinkResolver)
        plugin.config = {
            "general_settings": {
                "auto_install_fonts": False,
                "custom_font_path": "/tmp/custom-primary.ttf",
                "custom_emoji_font_path": "/tmp/custom-emoji.ttf",
            }
        }
        plugin._get_config_value = LinkResolver._get_config_value.__get__(
            plugin, LinkResolver
        )

        with (
            patch(
                "plugins.link_resolver.main.set_managed_fonts_enabled"
            ) as set_enabled,
            patch(
                "plugins.link_resolver.main.set_user_font_paths"
            ) as set_user_paths,
        ):
            LinkResolver._configure_managed_fonts(plugin)

        set_enabled.assert_called_once_with(False)
        set_user_paths.assert_called_once_with(
            "/tmp/custom-primary.ttf",
            "/tmp/custom-emoji.ttf",
        )
        self.assertFalse(plugin.font_auto_install_enabled)

    def test_refresh_config_defaults_xhs_prefer_ci_png_to_true(self):
        plugin = LinkResolver.__new__(LinkResolver)
        plugin.config = {}
        plugin.font_auto_install_enabled = False
        plugin.custom_primary_font_path = None
        plugin.custom_emoji_font_path = None
        plugin.xhs_renderer = object()
        plugin.weibo_extractor = type(
            "WeiboExtractorStub",
            (),
            {
                "set_cookie": lambda self, cookie: None,
                "has_user_cookie": lambda self: False,
            },
        )()
        plugin._get_config_value = LinkResolver._get_config_value.__get__(
            plugin, LinkResolver
        )

        with (
            patch.object(plugin, "_configure_managed_fonts", lambda: None),
            patch(
                "plugins.link_resolver.main.get_user_font_paths",
                return_value=ManagedFontPaths(primary=None, emoji=None),
            ),
            patch(
                "plugins.link_resolver.main.get_managed_font_paths",
                return_value=ManagedFontPaths(primary=None, emoji=None),
            ),
            patch(
                "plugins.link_resolver.main.find_default_font",
                return_value=None,
            ),
            patch(
                "plugins.link_resolver.main.find_emoji_font",
                return_value=None,
            ),
            patch(
                "plugins.link_resolver.main.XiaohongshuCardRenderer"
            ),
        ):
            LinkResolver._refresh_config(plugin)

        self.assertTrue(plugin.xhs_prefer_ci_png)

    def test_refresh_config_rebuilds_xhs_renderer_with_latest_font(self):
        plugin = LinkResolver.__new__(LinkResolver)
        plugin.config = {}
        plugin.font_auto_install_enabled = False
        plugin.custom_primary_font_path = None
        plugin.custom_emoji_font_path = None
        plugin.xhs_renderer = object()
        plugin.weibo_extractor = type(
            "WeiboExtractorStub",
            (),
            {
                "set_cookie": lambda self, cookie: None,
                "has_user_cookie": lambda self: False,
            },
        )()
        plugin._get_config_value = LinkResolver._get_config_value.__get__(
            plugin, LinkResolver
        )
        expected_font = Path("/tmp/latest-font.ttf")
        new_renderer = object()

        with (
            patch.object(plugin, "_configure_managed_fonts", lambda: None),
            patch(
                "plugins.link_resolver.main.get_user_font_paths",
                return_value=ManagedFontPaths(primary=None, emoji=None),
            ),
            patch(
                "plugins.link_resolver.main.get_managed_font_paths",
                return_value=ManagedFontPaths(primary=None, emoji=None),
            ),
            patch(
                "plugins.link_resolver.main.find_default_font",
                return_value=expected_font,
            ),
            patch(
                "plugins.link_resolver.main.find_emoji_font",
                return_value=None,
            ),
            patch(
                "plugins.link_resolver.main.XiaohongshuCardRenderer",
                return_value=new_renderer,
            ) as renderer_cls,
        ):
            LinkResolver._refresh_config(plugin)

        renderer_cls.assert_called_once_with(expected_font)
        self.assertIs(plugin.xhs_renderer, new_renderer)

    def test_install_managed_fonts_falls_back_to_next_source(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            primary_target = Path(tmpdir) / "NotoSansCJKsc-Regular.otf"
            emoji_target = Path(tmpdir) / "OpenMoji-black-glyf.ttf"
            requested_urls: list[str] = []

            def fake_stream(method: str, url: str, **kwargs):
                requested_urls.append(url)
                if "fastly.jsdelivr.net/gh/notofonts" in url:
                    raise httpx.ConnectError("mirror down")
                if "raw.githubusercontent.com/notofonts" in url:
                    return FakeResponse(b"primary-font")
                if "fastly.jsdelivr.net/gh/hfg-gmuend/openmoji" in url:
                    return FakeResponse(b"emoji-font")
                raise AssertionError(f"unexpected url: {url}")

            with (
                patch(
                    "plugins.link_resolver.core.common.font_manager.get_managed_primary_font_file",
                    return_value=primary_target,
                ),
                patch(
                    "plugins.link_resolver.core.common.font_manager.get_managed_emoji_font_file",
                    return_value=emoji_target,
                ),
                patch(
                    "plugins.link_resolver.core.common.font_manager.httpx.stream",
                    side_effect=fake_stream,
                ),
            ):
                installed = install_managed_fonts(timeout_sec=1.0)

            self.assertEqual(primary_target.read_bytes(), b"primary-font")
            self.assertEqual(emoji_target.read_bytes(), b"emoji-font")
            self.assertEqual(installed.primary, primary_target)
            self.assertEqual(installed.emoji, emoji_target)
            self.assertIn(
                "https://fastly.jsdelivr.net/gh/notofonts/noto-cjk@main/Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf",
                requested_urls,
            )
            self.assertIn(
                "https://raw.githubusercontent.com/notofonts/noto-cjk/main/Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf",
                requested_urls,
            )
            self.assertIn(
                "https://fastly.jsdelivr.net/gh/hfg-gmuend/openmoji@master/font/OpenMoji-black-glyf/OpenMoji-black-glyf.ttf",
                requested_urls,
            )

    def test_find_default_font_prefers_user_configured_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            user_font = Path(tmpdir) / "user.ttf"
            managed_font = Path(tmpdir) / "managed.ttf"
            user_font.write_bytes(b"user")
            managed_font.write_bytes(b"managed")

            with (
                patch(
                    "plugins.link_resolver.core.common.card_renderer.utils.get_user_font_paths",
                    return_value=ManagedFontPaths(primary=user_font, emoji=None),
                ),
                patch(
                    "plugins.link_resolver.core.common.card_renderer.utils.get_managed_font_paths",
                    return_value=ManagedFontPaths(primary=managed_font, emoji=None),
                ),
                patch(
                    "plugins.link_resolver.core.common.card_renderer.utils.managed_fonts_enabled",
                    return_value=True,
                ),
                patch(
                    "plugins.link_resolver.core.common.card_renderer.utils._font_path_loadable",
                    return_value=True,
                ),
            ):
                self.assertEqual(find_default_font(), user_font)

    def test_find_emoji_font_prefers_user_configured_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            user_font = Path(tmpdir) / "user-emoji.ttf"
            managed_font = Path(tmpdir) / "managed-emoji.ttf"
            user_font.write_bytes(b"user")
            managed_font.write_bytes(b"managed")

            with (
                patch(
                    "plugins.link_resolver.core.common.card_renderer.utils.get_user_font_paths",
                    return_value=ManagedFontPaths(primary=None, emoji=user_font),
                ),
                patch(
                    "plugins.link_resolver.core.common.card_renderer.utils.get_managed_font_paths",
                    return_value=ManagedFontPaths(primary=None, emoji=managed_font),
                ),
                patch(
                    "plugins.link_resolver.core.common.card_renderer.utils.managed_fonts_enabled",
                    return_value=True,
                ),
                patch(
                    "plugins.link_resolver.core.common.card_renderer.utils._emoji_font_path_renders",
                    return_value=True,
                ),
            ):
                self.assertEqual(find_emoji_font(), user_font)


if __name__ == "__main__":
    unittest.main(verbosity=2)
