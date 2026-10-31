# -*- coding: utf-8 -*-
"""MCP 工具定义：截屏 / 输入自动化 / 联网搜索 / 剪贴板 / 系统信息 / 通知。

只依赖 ``call.services`` 鸭子类型 bundle，模块顶层不 import 任何服务实现，
因此可以在没有 Tk、没有真实输入后端的测试环境里直接构造调用做单测。
"""

from __future__ import annotations

import json
import os
import platform
import sys
from dataclasses import asdict, is_dataclass
from typing import Any, Iterable

from .registry import ToolCall, ToolError, ToolRegistry, ToolResult, ToolSpec

__all__ = ["build_registry"]

CLIPBOARD_LIMIT = 20000          # 剪贴板读/写单次文本上限（字符）
INPUT_TEXT_LIMIT = 20000         # input.type 文本上限
INPUT_KEYS_LIMIT = 64            # 组合键字符串上限

_BUTTONS = {"left": "左键", "right": "右键", "middle": "中键"}

_COORD_DESC = (
    "虚拟屏幕绝对像素坐标（多显示器时可负）。先调用 screen.capture 确认屏幕布局。"
    " / Absolute virtual-screen pixel coordinate (may be negative); "
    "call screen.capture first to inspect the screen layout."
)


# ---------------------------------------------------------------- 通用工具


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _section(cfg: dict | None, key: str) -> dict:
    return _as_dict(_as_dict(cfg).get(key))


def _svc(call: ToolCall, name: str) -> Any:
    """从服务 bundle 取服务；同时兼容对象属性与 dict 两种形态。"""
    bundle = call.services
    obj = getattr(bundle, name, None) if bundle is not None else None
    if obj is None and isinstance(bundle, dict):
        obj = bundle.get(name)
    if obj is None:
        raise ToolError("服务未初始化：%s（MCP 运行时尚未装配完成）" % name,
                        code="service_missing")
    return obj


def _json_default(obj: Any) -> Any:
    """json.dumps 兜底：dataclass / 元组 / bytes 转成可序列化结构。"""
    if is_dataclass(obj) and not isinstance(obj, type):
        try:
            return asdict(obj)
        except Exception:
            return str(obj)
    if isinstance(obj, (tuple, list, set, frozenset)):
        return list(obj)
    if isinstance(obj, bytes):
        return "<%d bytes>" % len(obj)
    return str(obj)


def _dumps(obj: Any, *, indent: int | None = None) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=indent,
                      default=_json_default)


def _object_dict(obj: Any, keys: Iterable[str]) -> dict:
    """把 dataclass / dict / 普通对象统一成指定键的 dict。"""
    if isinstance(obj, dict):
        return {key: obj.get(key) for key in keys}
    if is_dataclass(obj) and not isinstance(obj, type):
        try:
            data = asdict(obj)
            return {key: data.get(key) for key in keys}
        except Exception:
            pass
    return {key: getattr(obj, key, None) for key in keys}


def _fail(text: str) -> ToolResult:
    """工具内可预期的失败（不抛异常，直接标记 isError）。"""
    result = ToolResult.text(text)
    result.is_error = True
    return result


def _rect4(value: Any) -> list[int] | None:
    """[x,y,w,h] / {x,y,w,h} / {x,y,width,height} → 四个 int。"""
    if isinstance(value, (list, tuple)) and len(value) == 4:
        try:
            return [int(v) for v in value]
        except (TypeError, ValueError):
            return None
    if isinstance(value, dict):
        names = ("x", "y", "w", "h")
        if not all(k in value for k in names):
            names = ("x", "y", "width", "height")
        if all(k in value for k in names):
            try:
                return [int(value[k]) for k in names]
            except (TypeError, ValueError):
                return None
    return None


def _pair(value: Any, default: tuple[int, int] = (0, 0)) -> tuple[int, int]:
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            return int(value[0]), int(value[1])
        except (TypeError, ValueError):
            return default
    return default


# ---------------------------------------------------------------- 参数校验


def _as_int(value: Any, label: str, *, default: int | None = None,
            minimum: int | None = None,
            maximum: int | None = None) -> int:
    if value is None:
        if default is None:
            raise ToolError("缺少参数 %s（需要整数）" % label, code="bad_params")
        value = default
    if isinstance(value, bool):
        raise ToolError("参数 %s 必须是整数，不能是布尔值" % label,
                        code="bad_params")
    if isinstance(value, int):
        number = value
    elif isinstance(value, float) and value.is_integer():
        number = int(value)
    elif isinstance(value, str):
        try:
            number = int(value.strip())
        except ValueError:
            raise ToolError("参数 %s 必须是整数" % label, code="bad_params")
    else:
        raise ToolError("参数 %s 必须是整数" % label, code="bad_params")
    if minimum is not None and number < minimum:
        raise ToolError("参数 %s 不能小于 %d（当前 %d）"
                        % (label, minimum, number), code="bad_params")
    if maximum is not None and number > maximum:
        raise ToolError("参数 %s 不能大于 %d（当前 %d）"
                        % (label, maximum, number), code="bad_params")
    return number


def _as_float(value: Any, label: str, *, default: float | None = None,
              minimum: float | None = None,
              maximum: float | None = None) -> float:
    if value is None:
        if default is None:
            raise ToolError("缺少参数 %s（需要数字）" % label, code="bad_params")
        value = default
    if isinstance(value, bool):
        raise ToolError("参数 %s 必须是数字，不能是布尔值" % label,
                        code="bad_params")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ToolError("参数 %s 必须是数字" % label, code="bad_params")
    if minimum is not None and number < minimum:
        raise ToolError("参数 %s 不能小于 %s（当前 %s）"
                        % (label, minimum, number), code="bad_params")
    if maximum is not None and number > maximum:
        raise ToolError("参数 %s 不能大于 %s（当前 %s）"
                        % (label, maximum, number), code="bad_params")
    return number


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "是", "开")
    return bool(value)


def _as_choice(value: Any, label: str, choices: Iterable[str],
               default: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ToolError("参数 %s 必须是字符串" % label, code="bad_params")
    text = value.strip()
    if not text:
        return default
    allowed = tuple(choices)
    if text not in allowed:
        raise ToolError("参数 %s 必须是 %s 之一（当前 %s）"
                        % (label, "/".join(allowed), text), code="bad_params")
    return text


def _as_text(value: Any, label: str, *, max_len: int,
             allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ToolError("参数 %s 必须是字符串" % label, code="bad_params")
    if not allow_empty and not value.strip():
        raise ToolError("参数 %s 不能为空" % label, code="bad_params")
    if len(value) > max_len:
        raise ToolError("参数 %s 超过 %d 字符上限（当前 %d）"
                        % (label, max_len, len(value)), code="too_long")
    return value


def _optional_xy(args: dict, label_x: str = "x",
                 label_y: str = "y") -> tuple[int | None, int | None]:
    """x/y 必须同时提供或同时省略；返回校验后的整数坐标。"""
    raw_x, raw_y = args.get(label_x), args.get(label_y)
    if (raw_x is None) != (raw_y is None):
        raise ToolError("参数 %s 和 %s 必须同时提供或同时省略"
                        % (label_x, label_y), code="bad_params")
    if raw_x is None:
        return None, None
    return (_as_int(raw_x, label_x), _as_int(raw_y, label_y))


# ---------------------------------------------------------------- 截屏工具


def _screen_capture(call: ToolCall) -> ToolResult:
    args = _as_dict(call.args)
    screen = _svc(call, "screen")

    mode = _as_choice(args.get("mode"), "mode",
                      ("full", "region", "window"), "full")
    monitor = _as_int(args.get("monitor"), "monitor", default=0,
                      minimum=0, maximum=16)

    region_tuple: tuple[int, int, int, int] | None = None
    raw_region = args.get("region")
    if raw_region is not None:
        if not isinstance(raw_region, (list, tuple)) or len(raw_region) != 4:
            raise ToolError("参数 region 必须是 [x, y, w, h] 四个整数",
                            code="bad_params")
        values = [_as_int(v, "region[%d]" % idx)
                  for idx, v in enumerate(raw_region)]
        if values[2] <= 0 or values[3] <= 0:
            raise ToolError("region 的宽和高必须大于 0", code="bad_params")
        # MCP 参数是 [x, y, w, h]；ScreenService.capture 收 (x1, y1, x2, y2)
        region_tuple = (values[0], values[1],
                        values[0] + values[2], values[1] + values[3])

    window_title: str | None = None
    if args.get("window_title") is not None:
        window_title = _as_text(args.get("window_title"), "window_title",
                                max_len=300)

    if mode == "region" and region_tuple is None:
        raise ToolError("mode=region 时必须提供 region 参数", code="bad_params")
    if mode == "window" and not window_title:
        raise ToolError("mode=window 时必须提供 window_title 参数",
                        code="bad_params")

    max_width: int | None = None
    if args.get("max_width") is not None:
        max_width = _as_int(args.get("max_width"), "max_width",
                            minimum=0, maximum=10000)
    else:
        cfg_max = _section(call.cfg, "screen").get("max_width")
        if isinstance(cfg_max, int) and not isinstance(cfg_max, bool) \
                and 0 <= cfg_max <= 10000:
            max_width = cfg_max

    try:
        result = screen.capture(mode=mode, monitor=monitor,
                                region=region_tuple,
                                window_title=window_title,
                                max_width=max_width)
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError("截屏失败：%s" % (exc or type(exc).__name__),
                        code="capture_failed") from exc

    png = getattr(result, "png", b"") or b""
    if not png:
        raise ToolError("截屏失败：服务没有返回 PNG 数据", code="capture_empty")

    scale = float(getattr(result, "scale", 1.0) or 1.0)
    if scale <= 0:
        scale = 1.0
    origin = _pair(getattr(result, "origin", (0, 0)))

    virtual_screen: list[int] | None = None
    try:
        info = screen.info()
        if isinstance(info, dict):
            virtual_screen = _rect4(info.get("virtual_screen"))
    except Exception:
        virtual_screen = None

    meta: dict = {
        "path": getattr(result, "path", None),
        "width": int(getattr(result, "width", 0) or 0),
        "height": int(getattr(result, "height", 0) or 0),
        "scale": scale,
        "origin": [origin[0], origin[1]],
        "virtual_screen": virtual_screen,
        "mode": str(getattr(result, "mode", mode) or mode),
    }
    window = getattr(result, "window", None)
    if window is not None:
        meta["window"] = _object_dict(
            window, ("hwnd", "title", "class_name", "pid", "rect"))

    text = (
        "截屏成功。图片像素到屏幕坐标："
        "screen_x = origin_x + img_x / scale。\n"
        + _dumps(meta, indent=2)
    )
    result_obj = ToolResult.image(png, text=text)
    result_obj.structured = meta
    return result_obj


def _screen_info(call: ToolCall) -> ToolResult:
    screen = _svc(call, "screen")
    try:
        info = screen.info()
    except Exception as exc:
        raise ToolError("读取屏幕信息失败：%s"
                        % (exc or type(exc).__name__),
                        code="screen_info_failed") from exc
    if not isinstance(info, (dict, list)):
        info = {"value": info}
    return ToolResult.text(_dumps(info, indent=2))


def _screen_list_windows(call: ToolCall) -> ToolResult:
    args = _as_dict(call.args)
    screen = _svc(call, "screen")
    limit = _as_int(args.get("limit"), "limit", default=50,
                    minimum=1, maximum=200)
    visible_only = _as_bool(args.get("visible_only"), True)
    include_self = _as_bool(args.get("include_self"), False)
    kwargs = {"limit": limit, "visible_only": visible_only}
    if include_self:
        kwargs["include_self"] = True      # 老 service 实现没有这个参数
    try:
        windows = screen.list_windows(**kwargs)
    except Exception as exc:
        raise ToolError("枚举窗口失败：%s" % (exc or type(exc).__name__),
                        code="list_windows_failed") from exc

    fields = ("hwnd", "title", "class_name", "pid", "rect")
    if include_self:
        fields += ("own",)
    items: list[dict] = []
    for window in list(windows or [])[:limit]:
        item = _object_dict(window, fields)
        rect = item.get("rect")
        if isinstance(rect, (list, tuple)):
            item["rect"] = [int(v) for v in _pair_rect(rect)]
        items.append(item)
    return ToolResult.text(_dumps(items, indent=2))


def _pair_rect(rect: Iterable[Any]) -> list[int]:
    out: list[int] = []
    for value in rect:
        try:
            out.append(int(value))
        except (TypeError, ValueError):
            out.append(0)
    return out


# ---------------------------------------------------------------- 输入工具


def _input_service(call: ToolCall) -> Any:
    """第二道防线：即使 server 已放行，服务未启用也拒绝执行。"""
    service = _svc(call, "input")
    if not bool(getattr(service, "enabled", False)):
        raise ToolError(
            "鼠标键盘自动化未启用：请在 VBHelper 面板「MCP 工具总线」中"
            "开启输入自动化后重试。",
            code="input_disabled")
    return service


def _call_input(func: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return func(*args, **kwargs)
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError("输入操作失败：%s" % (exc or type(exc).__name__),
                        code="input_failed") from exc


def _input_mouse_move(call: ToolCall) -> ToolResult:
    args = _as_dict(call.args)
    service = _input_service(call)
    x = _as_int(args.get("x"), "x")
    y = _as_int(args.get("y"), "y")
    duration = _as_float(args.get("duration"), "duration", default=0.0,
                         minimum=0.0, maximum=10.0)
    _call_input(service.mouse_move, x, y, duration)
    return ToolResult.text("鼠标已移动到 (%d, %d)（虚拟屏幕坐标）。" % (x, y))


def _input_mouse_click(call: ToolCall) -> ToolResult:
    args = _as_dict(call.args)
    service = _input_service(call)
    button = _as_choice(args.get("button"), "button",
                        ("left", "right", "middle"), "left")
    count = _as_int(args.get("count"), "count", default=1,
                    minimum=1, maximum=10)
    interval = _as_float(args.get("interval"), "interval", default=0.05,
                         minimum=0.0, maximum=2.0)
    x, y = _optional_xy(args)
    _call_input(service.mouse_click, button=button, count=count,
                interval=interval, x=x, y=y)
    where = "当前光标位置" if x is None else "(%d, %d)" % (x, y)
    return ToolResult.text("已在 %s 用%s点击 %d 次。"
                           % (where, _BUTTONS.get(button, button), count))


def _input_mouse_drag(call: ToolCall) -> ToolResult:
    args = _as_dict(call.args)
    service = _input_service(call)
    x1 = _as_int(args.get("x1"), "x1")
    y1 = _as_int(args.get("y1"), "y1")
    x2 = _as_int(args.get("x2"), "x2")
    y2 = _as_int(args.get("y2"), "y2")
    button = _as_choice(args.get("button"), "button",
                        ("left", "right", "middle"), "left")
    duration = _as_float(args.get("duration"), "duration", default=0.4,
                         minimum=0.0, maximum=10.0)
    _call_input(service.mouse_drag, x1, y1, x2, y2,
                button=button, duration=duration)
    return ToolResult.text("已从 (%d, %d) 拖拽到 (%d, %d)（%s，%.2f 秒）。"
                           % (x1, y1, x2, y2,
                              _BUTTONS.get(button, button), duration))


def _input_scroll(call: ToolCall) -> ToolResult:
    args = _as_dict(call.args)
    service = _input_service(call)
    amount = _as_int(args.get("amount"), "amount", default=-3,
                     minimum=-100, maximum=100)
    horizontal = _as_bool(args.get("horizontal"), False)
    x, y = _optional_xy(args)
    _call_input(service.scroll, amount, horizontal=horizontal, x=x, y=y)
    if amount == 0:
        action = "未滚动（amount=0）"
    elif horizontal:
        action = "已向%s滚动 %d 格" % ("右" if amount > 0 else "左",
                                       abs(amount))
    else:
        action = "已向%s滚动 %d 格" % ("上" if amount > 0 else "下",
                                       abs(amount))
    where = "" if x is None else "（光标在 (%d, %d)）" % (x, y)
    return ToolResult.text(action + where + "。")


def _input_type(call: ToolCall) -> ToolResult:
    args = _as_dict(call.args)
    service = _input_service(call)
    text = _as_text(args.get("text"), "text", max_len=INPUT_TEXT_LIMIT)
    method = _as_choice(args.get("method"), "method",
                        ("paste", "unicode"), "paste")
    interval = _as_float(args.get("interval"), "interval", default=0.0,
                         minimum=0.0, maximum=1.0)
    _call_input(service.type_text, text, method=method, interval=interval)
    how = "剪贴板粘贴" if method == "paste" else "Unicode 键入"
    return ToolResult.text("已输入 %d 个字符（%s）。" % (len(text), how))


def _input_key(call: ToolCall) -> ToolResult:
    args = _as_dict(call.args)
    service = _input_service(call)
    keys = _as_text(args.get("keys"), "keys", max_len=INPUT_KEYS_LIMIT)
    _call_input(service.press, keys)
    return ToolResult.text("已按下组合键：%s。" % keys)


# ---------------------------------------------------------------- 联网工具


_SEARCH_PROVIDERS = ("auto", "tavily", "brave", "serper", "searxng",
                     "bing_html", "baidu_html")


def _md_link_text(text: str) -> str:
    return (str(text or "（无标题）").replace("[", "［").replace("]", "］")
            .replace("\r", " ").replace("\n", " ").strip())


def _search_call(call: ToolCall) -> ToolResult:
    args = _as_dict(call.args)
    query = _as_text(args.get("query"), "query", max_len=500)
    limit = _as_int(args.get("limit"), "limit", default=8,
                    minimum=1, maximum=20)
    deep = _as_bool(args.get("deep"), False)
    max_pages = _as_int(args.get("max_pages"), "max_pages", default=3,
                        minimum=1, maximum=5)
    provider = _as_choice(args.get("provider"), "provider",
                          _SEARCH_PROVIDERS, "auto")

    service = _svc(call, "search")
    try:
        result = service.search(query, limit=limit, deep=deep,
                                max_pages=max_pages, provider=provider)
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError("搜索失败：%s（请检查网络或搜索服务配置）"
                        % (exc or type(exc).__name__),
                        code="search_failed") from exc

    provider_name = str(getattr(result, "provider", "") or provider)
    took_ms = int(getattr(result, "took_ms", 0) or 0)
    error = str(getattr(result, "error", "") or "").strip()
    hits = list(getattr(result, "hits", None) or [])
    pages = list(getattr(result, "pages", None) or [])

    hit_items = [_object_dict(hit, ("title", "url", "snippet", "provider"))
                 for hit in hits]
    page_items = [_object_dict(item, ("title", "url", "markdown", "error"))
                  for item in pages]

    if error and not hit_items and not page_items:
        return _fail("搜索失败：%s\n关键词：%s" % (error, query))
    if not hit_items and not page_items:
        return ToolResult.text(
            "## 搜索：%s\n\n"
            "没有找到结果。可以换一个更具体的关键词，或在 VBHelper 面板"
            "配置搜索 API Key / SearXNG 地址。" % query)

    lines = ["## 搜索：%s" % query, "",
             "- 提供方：%s / 耗时 %d ms"
             % (provider_name or "未知", took_ms),
             "- 命中：%d 条" % len(hit_items)]
    if error:
        lines.append("- 提示：%s（结果可能不完整）" % error)
    lines.append("")

    for index, hit in enumerate(hit_items, 1):
        title = _md_link_text(hit.get("title") or "")
        url = str(hit.get("url") or "").strip()
        lines.append("%d. [%s](%s)" % (index, title, url))
        snippet = str(hit.get("snippet") or "").strip()
        if snippet:
            lines.append("   %s" % " ".join(snippet.split()))
        lines.append("")

    if deep and page_items:
        lines.append("---")
        for index, page in enumerate(page_items, 1):
            title = _md_link_text(page.get("title") or "来源 %d" % index)
            url = str(page.get("url") or "").strip()
            body = str(page.get("markdown") or "").strip()
            page_error = str(page.get("error") or "").strip()
            lines.append("")
            lines.append("### 来源 %d: %s" % (index, title))
            if url:
                lines.append(url)
            lines.append("")
            if body:
                lines.append(body)
            elif page_error:
                lines.append("（抓取失败：%s）" % page_error)
            else:
                lines.append("（正文为空）")

    return ToolResult.text("\n".join(lines).rstrip() + "\n")


def _web_fetch(call: ToolCall) -> ToolResult:
    args = _as_dict(call.args)
    url = _as_text(args.get("url"), "url", max_len=2000)
    if not url.lower().startswith(("http://", "https://")):
        raise ToolError("参数 url 必须以 http:// 或 https:// 开头",
                        code="bad_params")
    max_chars = _as_int(args.get("max_chars"), "max_chars", default=20000,
                        minimum=500, maximum=100000)

    service = _svc(call, "search")
    try:
        page = service.fetch(url, max_chars=max_chars)
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError("抓取网页失败：%s（请检查网络）"
                        % (exc or type(exc).__name__),
                        code="fetch_failed") from exc

    title = str(getattr(page, "title", "") or "").strip() or url
    final_url = str(getattr(page, "url", "") or "").strip() or url
    body = str(getattr(page, "markdown", "") or "").strip()
    error = str(getattr(page, "error", "") or "").strip()

    if error and not body:
        return _fail("抓取失败：%s\nURL：%s\n原因：%s" % (title, final_url, error))

    lines = ["# %s" % title, "", final_url, ""]
    lines.append(body if body else
                 "（页面没有可提取的正文：可能不是 HTML 页面或内容为空。）")
    if error:
        lines.extend(["", "> 注意：%s" % error])
    return ToolResult.text("\n".join(lines).rstrip() + "\n")


# ---------------------------------------------------------------- RAG


def _rag_projects_text(rows: list[dict]) -> str:
    lines = ["## 已建立 RAG 索引的项目", ""]
    for row in rows:
        lines.append("- `%s` — %s 文件 / %s 块 · %s"
                     % (row.get("path"),
                        format(int(row.get("file_count") or 0), ","),
                        format(int(row.get("chunk_count") or 0), ","),
                        row.get("status") or "?"))
    return "\n".join(lines)


def _rag_search(call: ToolCall) -> ToolResult:
    args = _as_dict(call.args)
    query = _as_text(args.get("query"), "query", max_len=500)
    limit = _as_int(args.get("limit"), "limit", default=8,
                    minimum=1, maximum=20)
    project = args.get("project_path")
    if project is not None:
        project = _as_text(project, "project_path", max_len=4000)
    db = getattr(call.ctx, "db", None) if call.ctx is not None else None
    if db is None:
        raise ToolError("RAG 数据库不可用（VBHelper 存储底座未初始化）")
    try:
        from vbhelper.services.rag import engine
    except Exception as exc:
        raise ToolError("RAG 引擎加载失败：%r" % (exc,)) from exc
    if not project:
        app = getattr(call.ctx, "app", None)
        project = getattr(app, "root_dir", None)
    if not project:
        try:
            rows = engine.list_projects(db)
        except Exception:
            rows = []
        if len(rows) == 1:
            project = rows[0].get("path")
        elif rows:
            raise ToolError(
                "请指定 project_path；已建立索引的项目：\n"
                + "\n".join("- %s" % r.get("path") for r in rows[:12]))
        else:
            raise ToolError(
                "还没有建立 RAG 索引。请先在 VBHelper 面板选择项目文件夹建索引。")
    project = engine.norm_root(str(project))
    try:
        result = engine.search(db, project, query, limit=limit)
    except engine.RagError as exc:
        try:
            rows = engine.list_projects(db)
        except Exception:
            rows = []
        hint = ""
        if rows:
            hint = "\n已索引项目：\n" + "\n".join(
                "- %s" % r.get("path") for r in rows[:8])
        raise ToolError(str(exc) + hint, code="rag_not_indexed") from exc
    except Exception as exc:
        raise ToolError("RAG 检索失败：%r" % (exc,), code="rag_failed") from exc

    hits = result.get("hits") or []
    lines = ["## RAG 检索：%s" % result["query"], "",
             "项目：`%s`（%s 文件 / %s 块）"
             % (result["project"],
                format(int(result.get("files_indexed") or 0), ","),
                format(int(result.get("chunks_indexed") or 0), ","))]
    if not hits:
        lines.extend(["", "没有命中。可以换关键词，或先重建索引。"])
        return ToolResult.text("\n".join(lines))
    lines.append("")
    for index, hit in enumerate(hits, 1):
        location = "%s:%s-%s" % (hit.get("rel_path") or hit.get("path"),
                                 hit.get("start_line"), hit.get("end_line"))
        lines.append("%d. `%s`" % (index, location))
        snippet = " ".join(str(hit.get("snippet") or
                               hit.get("text") or "").split())
        if len(snippet) > 320:
            snippet = snippet[:320] + "…"
        lines.append("   %s" % snippet)
        lines.append("")
    if result.get("files"):
        lines.append("相关文件（只读这些）：")
        for rel in result["files"]:
            lines.append("- %s" % os.path.join(
                result["project"], rel.replace("/", os.sep)))
    lines.append("")
    lines.append("请只读取上述文件的相关行；不要用 shell 扫描整个项目。")
    return ToolResult.text("\n".join(lines), structured={
        "project": result["project"], "query": result["query"],
        "hits": hits, "files": result.get("files") or []})


def _rag_projects(call: ToolCall) -> ToolResult:
    db = getattr(call.ctx, "db", None) if call.ctx is not None else None
    if db is None:
        raise ToolError("RAG 数据库不可用（VBHelper 存储底座未初始化）")
    try:
        from vbhelper.services.rag import engine
        rows = engine.list_projects(db)
    except Exception as exc:
        raise ToolError("读取 RAG 项目失败：%r" % (exc,)) from exc
    if not rows:
        return ToolResult.text(
            "还没有建立 RAG 索引。请先在 VBHelper 面板选择项目文件夹建索引。")
    return ToolResult.text(_rag_projects_text(rows), structured={"projects": rows})


def _project_read_file(call: ToolCall) -> ToolResult:
    """读取项目根目录内的文本文件（配合 rag.search 的 path:line 使用）。"""
    args = _as_dict(call.args)
    raw_path = _as_text(args.get("path"), "path", max_len=4000)
    project = args.get("project_path")
    if project is not None:
        project = _as_text(project, "project_path", max_len=4000)
    if not project:
        app = getattr(call.ctx, "app", None) if call.ctx is not None else None
        project = getattr(app, "root_dir", None)
    if not project:
        raise ToolError("请提供 project_path，或在目录树里打开项目后再读取文件。")
    root = os.path.realpath(os.path.abspath(str(project)))
    target = raw_path
    if not os.path.isabs(target):
        target = os.path.join(root, target)
    target = os.path.realpath(os.path.abspath(target))
    try:
        common = os.path.commonpath([root, target])
    except ValueError:
        raise ToolError("路径超出项目根目录，拒绝读取。", code="path_escape")
    if os.path.normcase(common) != os.path.normcase(root):
        raise ToolError("路径超出项目根目录，拒绝读取。", code="path_escape")
    if not os.path.isfile(target):
        raise ToolError("文件不存在：%s" % target, code="not_found")
    base = os.path.basename(target).lower()
    if base.startswith(".") or base in (
            ".env", "secrets.json", "credentials.json", "id_rsa",
            "id_ed25519") or os.path.splitext(base)[1] in (
            ".pem", ".key", ".pfx", ".p12", ".keystore", ".jks"):
        raise ToolError("敏感文件不通过 MCP 读取：%s" % base,
                        code="sensitive_file")
    try:
        size = os.path.getsize(target)
    except OSError as exc:
        raise ToolError("读取文件失败：%r" % (exc,)) from exc
    if size > 1_048_576:
        raise ToolError("文件太大（%d 字节，上限 1MB）" % size,
                        code="file_too_large")
    try:
        with open(target, "rb") as fh:
            raw = fh.read(1_048_577)
    except OSError as exc:
        raise ToolError("读取文件失败：%r" % (exc,)) from exc
    if b"\x00" in raw[:8192]:
        raise ToolError("这是二进制文件，不支持文本读取。", code="binary_file")
    text = None
    for enc in ("utf-8-sig", "utf-8", "gb18030", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if text is None:
        raise ToolError("文件编码无法识别。", code="decode_failed")
    lines = text.splitlines()
    total = len(lines)
    start = _as_int(args.get("start_line"), "start_line", default=1,
                    minimum=1, maximum=1_000_000)
    end_raw = args.get("end_line")
    if end_raw is None:
        end = total
    else:
        end = _as_int(end_raw, "end_line", default=total, minimum=0,
                      maximum=1_000_000)
    if end <= 0:
        end = total
    if end < start:
        raise ToolError("end_line 必须大于等于 start_line", code="bad_params")
    end = min(end, total)
    selected = "\n".join(lines[start - 1:end])
    max_chars = _as_int(args.get("max_chars"), "max_chars", default=20000,
                        minimum=500, maximum=60000)
    truncated = False
    if len(selected) > max_chars:
        selected = selected[:max_chars]
        truncated = True
    rel = os.path.relpath(target, root).replace(os.sep, "/")
    header = ("# %s\n行 %d-%d / 共 %d 行" % (rel, start, end, total))
    if truncated:
        header += "（内容已截断到 %d 字符）" % max_chars
    return ToolResult.text(header + "\n\n" + selected, structured={
        "path": target, "rel_path": rel, "start_line": start,
        "end_line": end, "total_lines": total, "truncated": truncated})


# ---------------------------------------------------------------- 剪贴板


def _clipboard_read(call: ToolCall) -> ToolResult:
    service = _svc(call, "clipboard")
    try:
        text = service.read_text()
    except Exception as exc:
        raise ToolError("读取剪贴板失败：%s" % (exc or type(exc).__name__),
                        code="clipboard_read_failed") from exc
    text = "" if text is None else str(text)
    if not text:
        return ToolResult.text("（剪贴板为空，或当前内容不是文本。）")
    if len(text) > CLIPBOARD_LIMIT:
        return ToolResult.text(
            text[:CLIPBOARD_LIMIT]
            + "\n\n…（已截断：原文共 %d 字符，仅返回前 %d 字符）"
            % (len(text), CLIPBOARD_LIMIT))
    return ToolResult.text(text)


def _clipboard_write(call: ToolCall) -> ToolResult:
    args = _as_dict(call.args)
    service = _svc(call, "clipboard")
    text = _as_text(args.get("text"), "text", max_len=CLIPBOARD_LIMIT)
    try:
        service.write_text(text)
    except Exception as exc:
        raise ToolError("写入剪贴板失败：%s" % (exc or type(exc).__name__),
                        code="clipboard_write_failed") from exc
    return ToolResult.text("已写入剪贴板，共 %d 个字符。" % len(text))


# ---------------------------------------------------------------- 系统 / 通知


def _system_info(call: ToolCall) -> ToolResult:
    try:
        from vbhelper import __version__ as app_version
    except Exception:
        app_version = "1.0"
    try:
        import getpass
        user = getpass.getuser()
    except Exception:
        user = os.environ.get("USERNAME") or os.environ.get("USER") or ""

    root: str | None = None
    try:
        app = getattr(call.ctx, "app", None) if call.ctx is not None else None
        root_dir = getattr(app, "root_dir", None)
        if root_dir:
            root = str(root_dir)
    except Exception:
        root = None

    info: dict = {
        "app_version": str(app_version),
        "app_pid": os.getpid(),
        "app_running": True,
        "app_note": ("本次调用由 VBHelper 进程处理；窗口隐藏/缩到托盘不影响"
                     "MCP 工具。可用 screen.list_windows(include_self=true) "
                     "查看它自己的窗口。"),
        "python": platform.python_version(),
        "python_executable": sys.executable,
        "os": platform.platform(),
        "platform": sys.platform,
        "user": user,
        "project_root": root,
        "cwd": os.getcwd(),
    }

    try:
        screen = _svc(call, "screen")
        screen_info = screen.info()
        if isinstance(screen_info, dict):
            info["screen"] = {
                key: screen_info.get(key)
                for key in ("virtual_screen", "monitors", "dpi",
                            "primary_size", "active_window")
                if key in screen_info
            }
    except Exception as exc:
        info["screen_error"] = str(exc or type(exc).__name__)

    cfg = _as_dict(call.cfg)
    if cfg:
        info["mcp_enabled"] = bool(cfg.get("enabled", False))
        server_cfg = _as_dict(cfg.get("server"))
        if server_cfg:
            info["mcp_server"] = {
                str(key): value for key, value in server_cfg.items()
                if "token" not in str(key).lower()
            }
    return ToolResult.text(_dumps(info, indent=2))


def _notify_toast(call: ToolCall) -> ToolResult:
    args = _as_dict(call.args)
    title = _as_text(args.get("title"), "title", max_len=120)
    message = _as_text(args.get("message"), "message", max_len=2000)
    if call.notify is None:
        return ToolResult.text("通知通道不可用（当前运行环境没有注册桌面通知回调）。")
    try:
        accepted = call.notify(title, message)
    except Exception as exc:
        raise ToolError("发送通知失败：%s" % (exc or type(exc).__name__),
                        code="notify_failed") from exc
    if accepted is False:
        return _fail("通知未被面板接收（主界面可能已关闭或通知线程未就绪）。")
    return ToolResult.text("已发送桌面通知：%s" % title)


# ---------------------------------------------------------------- Schema


def _schema(description: str, properties: dict,
            required: Iterable[str] = ()) -> dict:
    return {
        "type": "object",
        "description": description,
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def _int_prop(desc: str, *, default: int | None = None,
              minimum: int | None = None,
              maximum: int | None = None) -> dict:
    prop: dict = {"type": "integer", "description": desc}
    if default is not None:
        prop["default"] = default
    if minimum is not None:
        prop["minimum"] = minimum
    if maximum is not None:
        prop["maximum"] = maximum
    return prop


def _num_prop(desc: str, *, default: float | None = None,
              minimum: float | None = None,
              maximum: float | None = None) -> dict:
    prop: dict = {"type": "number", "description": desc}
    if default is not None:
        prop["default"] = default
    if minimum is not None:
        prop["minimum"] = minimum
    if maximum is not None:
        prop["maximum"] = maximum
    return prop


def _str_prop(desc: str, *, default: str | None = None,
              enum: Iterable[str] | None = None) -> dict:
    prop: dict = {"type": "string", "description": desc}
    if default is not None:
        prop["default"] = default
    if enum is not None:
        prop["enum"] = list(enum)
    return prop


def _bool_prop(desc: str, *, default: bool | None = None) -> dict:
    prop: dict = {"type": "boolean", "description": desc}
    if default is not None:
        prop["default"] = default
    return prop


def _build_specs() -> list[ToolSpec]:
    """按契约表构造 15 个 ToolSpec（顺序稳定，便于面板展示）。"""
    return [
        ToolSpec(
            name="screen.capture",
            title="截屏 / Screen Capture",
            description=(
                "截取整个虚拟屏幕、指定显示器、屏幕区域或某个窗口，返回 PNG 图片"
                "和文本 JSON 元数据。返回文本第一句给出坐标换算，随后是 JSON："
                "path/width/height/scale/origin/virtual_screen/mode。"
                "坐标换算：screen_x = origin_x + img_x / scale，"
                "screen_y = origin_y + img_y / scale；origin 是截图左上角的"
                "虚拟屏幕坐标，图片像素除以 scale 得到屏幕像素。"
                "max_width=0 表示不缩放，默认按面板配置缩放（通常 1600）。"
                " / Capture the virtual screen, one monitor, a region or a "
                "window as PNG plus JSON metadata. Convert image pixels to "
                "screen coordinates with screen_x = origin_x + img_x / scale. "
                "max_width=0 disables downscaling."
            ),
            schema=_schema(
                "截屏参数。region 使用虚拟屏幕坐标；mode=window 时按标题"
                "（不区分大小写、支持子串）查找窗口。 / Capture options.",
                {
                    "mode": _str_prop(
                        "截图模式：full=整个虚拟屏幕，region=指定区域，"
                        "window=指定窗口。 / Capture mode.",
                        default="full", enum=("full", "region", "window")),
                    "monitor": _int_prop(
                        "显示器序号（0 开始），仅 mode=full 时用于指定某块屏幕；"
                        "默认 0。 / Monitor index for mode=full.",
                        default=0, minimum=0, maximum=16),
                    "region": {
                        "type": "array",
                        "description": "mode=region 时的区域 [x, y, w, h]，"
                                       "虚拟屏幕绝对坐标。 / Region [x,y,w,h].",
                        "items": {"type": "integer"},
                        "minItems": 4,
                        "maxItems": 4,
                    },
                    "window_title": _str_prop(
                        "mode=window 时的窗口标题子串（不区分大小写）。"
                        " / Window title substring for mode=window."),
                    "max_width": _int_prop(
                        "输出图片最大宽度（像素）；0 表示不缩放；缺省使用面板配置。"
                        " / Max output width; 0 = no downscale.",
                        minimum=0, maximum=10000),
                },
                required=(),
            ),
            handler=_screen_capture,
            group="screen",
            permission="safe",
            default_enabled=True,
            timeout=30.0,
            rate_limit="60/min",
        ),
        ToolSpec(
            name="screen.info",
            title="屏幕信息 / Screen Info",
            description=(
                "返回虚拟屏幕范围、显示器列表、DPI、主屏尺寸和当前活动窗口。"
                " / Virtual screen bounds, monitor list, DPI, primary size and "
                "active window."
            ),
            schema=_schema("无参数。 / No arguments.", {}, ()),
            handler=_screen_info,
            group="screen",
            permission="safe",
            default_enabled=True,
            timeout=10.0,
            rate_limit="120/min",
        ),
        ToolSpec(
            name="screen.list_windows",
            title="窗口列表 / List Windows",
            description=(
                "列出当前可见的顶层窗口（hwnd/title/class_name/pid/rect），"
                "rect 为虚拟屏幕坐标 l,t,r,b，可用于 screen.capture 的 "
                "window_title 或输入工具定位。 / List visible top-level "
                "windows with virtual-screen rects."
            ),
            schema=_schema(
                "窗口枚举参数。 / Window listing options.",
                {
                    "limit": _int_prop(
                        "最多返回多少个窗口。 / Maximum number of windows.",
                        default=50, minimum=1, maximum=200),
                    "visible_only": _bool_prop(
                        "是否只返回可见窗口。 / Only visible windows.",
                        default=True),
                    "include_self": _bool_prop(
                        "是否包含 VBHelper 自己的窗口（own=true），"
                        "用于确认应用是否在后台运行。 / Include VBHelper's "
                        "own windows (own=true).",
                        default=False),
                },
                required=(),
            ),
            handler=_screen_list_windows,
            group="screen",
            permission="safe",
            default_enabled=True,
            timeout=10.0,
            rate_limit="60/min",
        ),
        ToolSpec(
            name="input.mouse_move",
            title="移动鼠标 / Move Mouse",
            description=(
                "把鼠标移动到虚拟屏幕绝对坐标。坐标可为负（多显示器）。"
                "需要先在面板开启输入自动化；不确定位置时先用 screen.capture "
                "截图。 / Move the mouse to absolute virtual-screen coordinates. "
                "Input automation must be enabled in the panel."
            ),
            schema=_schema("移动鼠标参数。 / Mouse move options.", {
                "x": _int_prop("目标 X，虚拟屏幕绝对像素（可负）。 / Target X. "),
                "y": _int_prop("目标 Y，虚拟屏幕绝对像素（可负）。 / Target Y."),
                "duration": _num_prop(
                    "移动耗时秒数；0 表示瞬移。 / Duration in seconds; 0=instant.",
                    default=0.0, minimum=0.0, maximum=10.0),
            }, required=("x", "y")),
            handler=_input_mouse_move,
            group="input",
            permission="confirm",
            default_enabled=True,
            timeout=15.0,
            rate_limit="120/min",
        ),
        ToolSpec(
            name="input.mouse_click",
            title="鼠标点击 / Mouse Click",
            description=(
                "在指定坐标（或当前光标处）点击鼠标。坐标是虚拟屏幕绝对像素，"
                "可负；需要先在面板开启输入自动化。 / Click the mouse at "
                "absolute virtual-screen coordinates or at the current cursor."
            ),
            schema=_schema(
                "鼠标点击参数；x/y 要么都提供要么都省略（省略=当前位置）。"
                " / Click options.",
                {
                    "x": _int_prop("点击 X，虚拟屏幕绝对像素（可负）。"
                                   " / Click X."),
                    "y": _int_prop("点击 Y，虚拟屏幕绝对像素（可负）。"
                                   " / Click Y."),
                    "button": _str_prop(
                        "按键：left/right/middle。 / Button.",
                        default="left", enum=("left", "right", "middle")),
                    "count": _int_prop(
                        "点击次数（1-10）。 / Click count.",
                        default=1, minimum=1, maximum=10),
                    "interval": _num_prop(
                        "连击间隔秒数。 / Interval between clicks.",
                        default=0.05, minimum=0.0, maximum=2.0),
                },
                required=(),
            ),
            handler=_input_mouse_click,
            group="input",
            permission="confirm",
            default_enabled=True,
            timeout=30.0,
            rate_limit="120/min",
        ),
        ToolSpec(
            name="input.mouse_drag",
            title="鼠标拖拽 / Mouse Drag",
            description=(
                "按住鼠标从 (x1,y1) 拖到 (x2,y2)，坐标是虚拟屏幕绝对像素。"
                "需要先在面板开启输入自动化。 / Drag from (x1,y1) to (x2,y2) "
                "in absolute virtual-screen coordinates."
            ),
            schema=_schema("拖拽参数。 / Drag options.", {
                "x1": _int_prop("起点 X。 / Start X."),
                "y1": _int_prop("起点 Y。 / Start Y."),
                "x2": _int_prop("终点 X。 / End X."),
                "y2": _int_prop("终点 Y。 / End Y."),
                "button": _str_prop(
                    "拖拽按键：left/right/middle。 / Drag button.",
                    default="left", enum=("left", "right", "middle")),
                "duration": _num_prop(
                    "拖拽耗时秒数。 / Drag duration in seconds.",
                    default=0.4, minimum=0.0, maximum=10.0),
            }, required=("x1", "y1", "x2", "y2")),
            handler=_input_mouse_drag,
            group="input",
            permission="confirm",
            default_enabled=True,
            timeout=30.0,
            rate_limit="120/min",
        ),
        ToolSpec(
            name="input.scroll",
            title="滚动滚轮 / Scroll",
            description=(
                "滚动鼠标滚轮；amount 正数向上、负数向下，水平模式正数向右。"
                "可指定光标位置。需要先在面板开启输入自动化。 / Scroll the "
                "wheel (positive = up/right, negative = down/left)."
            ),
            schema=_schema("滚轮参数。 / Scroll options.", {
                "amount": _int_prop(
                    "滚动格数，正上负下（水平模式正右负左）。 / Wheel amount.",
                    default=-3, minimum=-100, maximum=100),
                "horizontal": _bool_prop(
                    "是否水平滚动。 / Horizontal scrolling.",
                    default=False),
                "x": _int_prop("滚动前移动到的 X（可选）。 / Optional cursor X."),
                "y": _int_prop("滚动前移动到的 Y（可选）。 / Optional cursor Y."),
            }, required=()),
            handler=_input_scroll,
            group="input",
            permission="confirm",
            default_enabled=True,
            timeout=30.0,
            rate_limit="120/min",
        ),
        ToolSpec(
            name="input.type",
            title="键盘输入文本 / Type Text",
            description=(
                "向当前焦点窗口输入文本（中文/Emoji 也能输入）。默认 paste 方式："
                "备份剪贴板→写入→Ctrl+V→恢复；unicode 方式逐字符模拟键盘。"
                "需要先在面板开启输入自动化。 / Type text into the focused "
                "window (paste by default, unicode optional)."
            ),
            schema=_schema(
                "文本输入参数；text 最长 20000 字符。 / Text input options.",
                {
                    "text": _str_prop(
                        "要输入的文本（非空，≤20000 字符）。 / Text to type."),
                    "method": _str_prop(
                        "输入方式：paste=剪贴板粘贴（快），unicode=模拟键击。"
                        " / Input method.",
                        default="paste", enum=("paste", "unicode")),
                    "interval": _num_prop(
                        "unicode 模式每个字符的间隔秒数。 / Per-char delay.",
                        default=0.0, minimum=0.0, maximum=1.0),
                },
                required=("text",),
            ),
            handler=_input_type,
            group="input",
            permission="confirm",
            default_enabled=True,
            timeout=60.0,
            rate_limit="60/min",
        ),
        ToolSpec(
            name="input.key",
            title="按键 / Press Keys",
            description=(
                "按下组合键，例如 ctrl+shift+t、alt+f4、enter、win。需要先在"
                "面板开启输入自动化。 / Press a key combination such as "
                "ctrl+shift+t or enter."
            ),
            schema=_schema(
                "组合键参数；用 + 连接，例如 ctrl+shift+t（≤64 字符）。"
                " / Key combination.",
                {
                    "keys": _str_prop(
                        "组合键字符串，用 + 连接。 / Key combination string."),
                },
                required=("keys",),
            ),
            handler=_input_key,
            group="input",
            permission="confirm",
            default_enabled=True,
            timeout=15.0,
            rate_limit="120/min",
        ),
        ToolSpec(
            name="web.search",
            title="联网搜索 / Web Search",
            description=(
                "联网搜索并返回 Markdown 结果列表。deep=true 时会抓取前几页正文"
                "（已截断）附在结果后面。支持 Tavily/Brave/Serper/SearXNG 或"
                "Bing/Baidu HTML 兜底。 / Search the web; deep=true also "
                "fetches the top pages as Markdown."
            ),
            schema=_schema(
                "搜索参数。 / Search options.",
                {
                    "query": _str_prop(
                        "搜索关键词（≤500 字符）。 / Search query."),
                    "limit": _int_prop(
                        "返回结果条数。 / Number of results.",
                        default=8, minimum=1, maximum=20),
                    "deep": _bool_prop(
                        "是否抓取前几页正文（更慢但信息更全）。"
                        " / Fetch top pages' full text.",
                        default=False),
                    "max_pages": _int_prop(
                        "deep=true 时抓取的页数。 / Pages to fetch in deep mode.",
                        default=3, minimum=1, maximum=5),
                    "provider": _str_prop(
                        "搜索提供方；auto 自动选择。 / Search provider.",
                        default="auto", enum=_SEARCH_PROVIDERS),
                },
                required=("query",),
            ),
            handler=_search_call,
            group="web",
            permission="safe",
            default_enabled=True,
            timeout=60.0,
            rate_limit="30/min",
        ),
        ToolSpec(
            name="web.fetch",
            title="抓取网页 / Fetch Page",
            description=(
                "抓取一个网页并转换为 Markdown（标题、正文、链接、代码块、列表）。"
                " / Fetch a web page and convert it to Markdown."
            ),
            schema=_schema(
                "抓取参数。 / Fetch options.",
                {
                    "url": _str_prop(
                        "要抓取的 http/https 地址。 / Page URL."),
                    "max_chars": _int_prop(
                        "正文最大字符数。 / Maximum characters to return.",
                        default=20000, minimum=500, maximum=100000),
                },
                required=("url",),
            ),
            handler=_web_fetch,
            group="web",
            permission="safe",
            default_enabled=True,
            timeout=45.0,
            rate_limit="60/min",
        ),
        ToolSpec(
            name="clipboard.read",
            title="读取剪贴板 / Read Clipboard",
            description=(
                "读取当前剪贴板文本；超过 20000 字符会截断并提示。"
                " / Read clipboard text (truncated at 20000 characters)."
            ),
            schema=_schema("无参数。 / No arguments.", {}, ()),
            handler=_clipboard_read,
            group="clipboard",
            permission="safe",
            default_enabled=True,
            timeout=10.0,
            rate_limit="60/min",
        ),
        ToolSpec(
            name="clipboard.write",
            title="写入剪贴板 / Write Clipboard",
            description=(
                "把文本写入系统剪贴板（≤20000 字符）。"
                " / Write text to the system clipboard."
            ),
            schema=_schema(
                "剪贴板写入参数。 / Clipboard write options.",
                {
                    "text": _str_prop(
                        "要写入的文本（非空，≤20000 字符）。 / Text to write."),
                },
                required=("text",),
            ),
            handler=_clipboard_write,
            group="clipboard",
            permission="safe",
            default_enabled=True,
            timeout=10.0,
            rate_limit="60/min",
        ),
        ToolSpec(
            name="system.info",
            title="系统信息 / System Info",
            description=(
                "返回应用版本、Python/系统版本、用户名、项目根目录、当前工作目录、"
                "屏幕信息和 MCP 服务状态（不含 token）。"
                " / Return app/Python/OS/user/project/screen info and MCP "
                "status (no token)."
            ),
            schema=_schema("无参数。 / No arguments.", {}, ()),
            handler=_system_info,
            group="system",
            permission="safe",
            default_enabled=True,
            timeout=10.0,
            rate_limit="60/min",
        ),
        ToolSpec(
            name="notify.toast",
            title="桌面通知 / Desktop Notification",
            description=(
                "在 VBHelper 面板上显示一条桌面通知。"
                " / Show a desktop notification via the VBHelper panel."
            ),
            schema=_schema(
                "通知参数。 / Notification options.",
                {
                    "title": _str_prop(
                        "通知标题（≤120 字符）。 / Notification title."),
                    "message": _str_prop(
                        "通知正文（≤2000 字符）。 / Notification message."),
                },
                required=("title", "message"),
            ),
            handler=_notify_toast,
            group="notify",
            permission="safe",
            default_enabled=True,
            timeout=10.0,
            rate_limit="30/min",
        ),
        ToolSpec(
            name="rag.search",
            title="RAG 项目检索 / Search Project Index",
            description=(
                "在已建立的本地 RAG 索引里检索代码/文档，返回命中的 "
                "相对路径:行号、片段和相关文件列表。只读取返回的文件/行，"
                "不要用 shell 扫描整个项目。project_path 省略时使用当前目录树"
                "根目录或唯一的已索引项目。 / Search the local project RAG "
                "index; returns path:line hits and snippets. Read only those "
                "files instead of scanning the disk."
            ),
            schema=_schema(
                "RAG 检索参数。 / RAG search options.",
                {
                    "query": _str_prop(
                        "检索词/问题（关键词或短语，中英文均可）。 / Query."),
                    "project_path": _str_prop(
                        "项目根目录（可选；省略时用当前根目录或唯一项目）。 / "
                        "Optional project root."),
                    "limit": _int_prop(
                        "最多命中片段数。 / Maximum hits.",
                        default=8, minimum=1, maximum=20),
                },
                required=("query",),
            ),
            handler=_rag_search,
            group="rag",
            permission="safe",
            default_enabled=True,
            timeout=20.0,
            rate_limit="60/min",
        ),
        ToolSpec(
            name="rag.projects",
            title="已索引项目 / List Indexed Projects",
            description=(
                "列出已建立 RAG 索引的项目、文件/块数量和最后索引时间。 / "
                "List indexed projects with stats."
            ),
            schema=_schema("无参数。 / No arguments.", {}, required=()),
            handler=_rag_projects,
            group="rag",
            permission="safe",
            default_enabled=True,
            timeout=10.0,
            rate_limit="60/min",
        ),
        ToolSpec(
            name="project.read_file",
            title="读取项目文件 / Read Project File",
            description=(
                "读取项目根目录内的文本文件（可指定 start_line/end_line），"
                "配合 rag.search 返回的 path:line 使用；路径不能逃出项目根目录，"
                "敏感文件（.env/密钥）拒绝。只读，不修改文件。 / Read a text "
                "file inside the project root; supports line ranges; read-only."
            ),
            schema=_schema(
                "项目文件读取参数。 / Project file read options.",
                {
                    "path": _str_prop(
                        "绝对路径或相对项目根目录的路径。 / Absolute or "
                        "project-relative path."),
                    "project_path": _str_prop(
                        "项目根目录（可选；省略时用当前目录树根）。 / "
                        "Optional project root."),
                    "start_line": _int_prop(
                        "起始行（1 起）。 / Start line (1-based).",
                        default=1, minimum=1),
                    "end_line": _int_prop(
                        "结束行（含）；省略到文件末尾。 / End line inclusive.",
                        minimum=0),
                    "max_chars": _int_prop(
                        "最多返回字符数。 / Max characters.",
                        default=20000, minimum=500, maximum=60000),
                },
                required=("path",),
            ),
            handler=_project_read_file,
            group="project",
            permission="safe",
            default_enabled=True,
            timeout=15.0,
            rate_limit="120/min",
        ),
    ]


def build_registry(services: Any = None) -> ToolRegistry:
    """构造包含契约表 18 个工具的注册表。

    ``services`` 参数保留给契约签名；handler 执行时统一从
    ``ToolCall.services`` 取服务，避免把服务实例和 schema 定义绑死。
    """
    del services                       # 仅保持契约签名，实际在调用时注入
    registry = ToolRegistry()
    for spec in _build_specs():
        registry.register(spec)
    return registry
