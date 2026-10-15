# 宣传素材交付

- `B站录屏宣传页.html`：内嵌图片的单文件 HTML，六章演示、全屏、录屏模式、键盘翻页；离线展示可用。
- `index.html`：便于修改与 GitHub 浏览的源页面，图片放在 assets。
- `B站录制脚本.md`：约 90 秒分镜、标题与简介。
- `小黑盒图文稿.md`：配图嵌入正文，末尾附图片上传顺序；由用户自行在小黑盒发布。
- `assets/bilibili-cover.png`：1920×1080 视频封面。
- `assets/heybox-01-cover.png`：1600×1600 图文封面。
- `assets/heybox-02-receive.png`、`heybox-03-reply.png`、`heybox-04-install.png`：功能和使用配图。
- `assets/app-*.png`：真实软件窗口，采用临时演示数据，没有调用真实 API。
- `assets/city-radio.png`：使用内置 imagegen 创作的原创氛围插画；非游戏截图。提示词见 `image-prompt.txt`。

GitHub 链接按用户指定的 `piaobo2233/deadlock-translato` 与测试标签 `v1.0.0-test.1` 配置。发布前需确认源码、Release 和资产链接实际可访问。

所有图文均标明测试版、自备 API 与未验收边界。页面对 1920×1080 桌面和 390×844 手机布局、五语切换、键盘、全屏、单文件图片加载完成自动检查。录制真实对局效果需另拍实际游戏，不能用示意页面冒充实测。
