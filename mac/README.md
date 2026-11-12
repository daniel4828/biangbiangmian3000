# Biangbiang Quick（Mac 菜单栏壳）

一个很小的菜单栏应用：在任何程序里按 **⌥⇧K**，弹出一个悬浮面板，里面是服务器的 `/quick` 页面（词典聊天）。原生部分只是外壳。

## 安装

```bash
bash mac/install.sh
```

编译、安装到 `/Applications/Biangbiang Quick.app`、自签名并启动。

## 使用

- 第一次打开面板时，在面板里登录一次（登录 Cookie 会保存，重启后仍有效）。
- ⌥⇧K 显示/隐藏；Esc 或点击别处也会隐藏。
- 菜单栏图标菜单：开机自启、修改服务器地址、重新加载、退出。
- 服务器地址默认 `https://biangbiangmian3000.danielschreiber.eu`，可在菜单「Server-Adresse…」修改（本地开发可填 `http://localhost:8000`）。
- 如果菜单里出现「⚠ ⌥⇧K belegt」，说明这个快捷键被别的程序占用了。
