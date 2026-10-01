"""
联网自动更新（只有安装版会下载、替换文件；源码运行只提示有新版本）

- 更新源：GitHub Releases。SOURCES 是列表，按顺序尝试；以后要加 Gitee 等返回同样格式的源，往里加一项即可
- 发布：tools/release.py 打包后生成 manifest.json（每个程序文件的 SHA256）、签名、补丁包，上传到 GitHub 草稿
- 客户端：client.py 后台检查、下载、校验；apply_update.ps1 在程序退出后替换文件，再重新打开程序
- 清单用 Ed25519 签名，私钥只在发布者本机，程序里只有下面的公钥
"""

import os

REPO = "AllenPan-Git/SimpleQuant"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
# 返回 GitHub「最新发布」接口格式（tag_name / body / html_url / assets[].name, browser_download_url）的地址
SOURCES = [f"https://api.github.com/repos/{REPO}/releases/latest"]
if os.environ.get("SIMPLEQUANT_UPDATE_SOURCE"):          # 测试用：指向本机的模拟服务器（签名照样验证）
    SOURCES = [os.environ["SIMPLEQUANT_UPDATE_SOURCE"]]

PUBLIC_KEY = "b187ff9ca030ba4a37dc14c95a32bf6b4e15fdb5b5e865a77b25181bd87e3216"   # Ed25519 公钥（十六进制），由 tools/release.py keygen 生成后填入
