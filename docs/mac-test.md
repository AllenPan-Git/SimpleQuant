# SimpleQuant macOS 真机测试清单

用于在 Mac 上验证安装版。每一步如有异常，请截图（`Cmd + Shift + 4` 选取区域，`Cmd + Shift + 3` 全屏，图片保存在桌面），并记下步骤编号。全部完成约需 20～30 分钟。

## 0. 准备
1. 点左上角苹果图标 →「关于本机」，记下**芯片**（Apple M1/M2/M3… 或 Intel）和 **macOS 版本**。
2. 用浏览器打开 GitHub 上最新一次 Actions 运行（**Actions → Build macOS / Linux**，选最新一条成功的记录），登录后在页面底部 **Artifacts** 下载：
   - Apple 芯片：`SimpleQuant-macos-arm64`
   - Intel 芯片：`SimpleQuant-macos-x86_64`
3. 下载得到 zip，双击解压，在 `dist/macos/` 下找到 `SimpleQuant-<版本>-macos-<芯片>.dmg`。

## 1. 安装与首次打开
1. 双击 dmg，将 SimpleQuant 拖到「Applications / 应用程序」。若提示没有权限（非管理员账户），改为拖到桌面，并记下这一点。
2. 双击打开 SimpleQuant。**记录系统给出的提示原文并截图**（例如「无法验证开发者」「已损坏」）。
3. 按 README 中的方法放行：「系统设置 → 隐私与安全性」→ 页面下方「仍要打开」；不行再用终端：
   `xattr -dr com.apple.quarantine /Applications/SimpleQuant.app`
   记录哪种方法有效。
4. 程序窗口打开后截图首页。检查：中文字体正常、标题使用宋体风格、右上角可切换「中 · EN」和深色主题。

## 2. 基本功能
1. **数据页**：用 AKShare 下载 `510300`（沪深 300 ETF），区间默认即可。记录是否成功、耗时。
2. **策略页**：选择模板「双均线交叉」，点「使用此策略回测」。
3. **回测页**：运行回测，检查收益曲线、指标、成交表都有显示；切换到「Backtrader 原生图」，检查图上的中文不是方框。截图。
4. **导出**：在回测页点导出 Python 脚本，检查是否弹出保存对话框、能否保存到桌面。
5. **设置页**：底部「关于 SimpleQuant」显示版本号及项目主页、问题反馈、更新记录链接；点「检查更新」，记录提示。

## 3. 模拟盘与定时任务
1. 模拟盘页新建一个账户（用刚才的双均线交叉策略和 510300）。
2. 展开「每日自动运行（macOS launchd）」，点「创建定时任务」，记录提示，并确认显示「已设置每日自动运行」。
3. 再点「删除定时任务」，确认恢复为「尚未设置自动运行」。

## 4. 退出
1. 关闭窗口，检查程序是否完全退出（程序坞里的图标不再显示运行标记）。
2. 再次打开一次，确认第二次打开不再被系统拦截。

## 5. 生成检查结果
打开「终端」，运行（整行复制）：

```
curl -fsSL https://raw.githubusercontent.com/AllenPan-Git/SimpleQuant/ci/cross-platform/tools/mac_check.sh | sh
```

桌面上会生成 `SimpleQuant-检查结果.txt`。将它和截图一起带回（U 盘或邮件发给自己）。

## 6. 清理（借用的电脑，务必执行）
退出 SimpleQuant 后在终端运行：

```
curl -fsSL https://raw.githubusercontent.com/AllenPan-Git/SimpleQuant/ci/cross-platform/tools/mac_check.sh | sh -s clean
```

它会删除程序、数据目录（`~/Library/Application Support/SimpleQuant`）和定时任务。最后手动删除「下载」中的 zip / dmg、桌面上的截图和检查结果（已拷走后）。

## 可选：源码版
若电脑已安装 Python 3.10 及以上（终端运行 `python3 --version` 查看）：

```
git clone https://github.com/AllenPan-Git/SimpleQuant.git
cd SimpleQuant && sh start.sh
```

首次运行会下载依赖（约 300 MB），之后打开窗口。测试完删除 `SimpleQuant` 文件夹即可（源码版数据在该文件夹内）。
