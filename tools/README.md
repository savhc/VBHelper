# tools

开发期工具，不参与打包，不进 `VBHelper.exe`。

| 目录 | 内容 |
|---|---|
| `checks/` | 无 GUI 自检与冒烟脚本。命名 `_<模块>_check.py` 对应被测模块，`_<模块>_shot.py` 出截图到 `_shots/`。 |
| `diag/` | 一次性诊断脚本，用于复现线上问题；不保证长期可用。 |
| `legacy/` | 历史存档。`vbhelper_singlefile.py` 是拆包前的单文件版本，只作对照，不参与构建。 |
| `make_icon.py` | 生成 `vbhelper/assets/vbhelper.ico` 与 `vbhelper_256.png`。改配色后重跑即可。 |

## 运行约定

所有脚本从项目根目录执行，脚本内部自行把根目录加入 `sys.path`：

```bash
python tools/checks/_store_check.py
python tools/checks/_rag_check.py
```

GUI 相关脚本会创建真实 Tk 窗口，其余脚本不建窗口、不联网（除非脚本说明里注明）。
