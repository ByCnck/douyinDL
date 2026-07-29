"""抖音视频/合集下载工具

基于 f2 库的底层 crawler，绕过 f2 handler 中需要登录 cookie 的用户信息接口，
直接调用合集 API（仅需 ttwid 匿名 token + a_bogus 签名），获取合集内全部视频
列表并下载无水印视频文件。
"""

from douyindl.downloader import DouyinDownloader, main

__all__ = ["DouyinDownloader", "main"]
