# -*- coding: utf-8 -*-
"""VBHelper 打包/启动入口。

开发运行：python run.py
打包：python -m PyInstaller --onefile --noconsole --name VBHelper run.py
MCP stdio：VBHelper.exe --mcp-stdio（不建 Tk，只跑 JSON-RPC 桥）
"""
import sys


def _main() -> int:
    argv = sys.argv[1:]
    if "--mcp-stdio" in argv:
        # 必须在 import app 之前分流：stdio 桥不能加载 Tk/主程序
        from vbhelper.mcp_stdio import main as stdio_main
        rest = [a for a in argv if a != "--mcp-stdio"]
        return stdio_main(rest)
    from vbhelper.app import main as app_main
    app_main()
    return 0


if __name__ == "__main__":
    sys.exit(_main())
