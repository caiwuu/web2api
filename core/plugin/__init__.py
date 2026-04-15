"""插件层：抽象接口与注册表，各 type 实现 create_page  / create_conversation / stream_completion。"""

from core.plugin.base import AbstractPlugin, BaseSitePlugin, PluginRegistry, SiteConfig

__all__ = ["AbstractPlugin", "BaseSitePlugin", "PluginRegistry", "SiteConfig"]
