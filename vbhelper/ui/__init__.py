# -*- coding: utf-8 -*-
"""vbhelper.ui — 可复用 UI 基础框架（DPI 缩放 / 主题 / PIL 渲染 / 图标）

保持包级导入轻量：子模块按需 import，不在 __init__ 里预加载，
避免 feature 关闭时拖慢启动（懒加载原则）。

用法示例::

    from vbhelper.ui import scale, theme
    scale.init(root)
    tm = theme.ThemeManager(root, cfg)
"""
