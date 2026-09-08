[![Open in Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/drive/1l1fAyDzNSSCVOF_JBpXRp2b3SHuI5bz6?usp=sharing) CAPTCHA model source: [captcha-cloudrun](https://github.com/GitHub30/captcha-cloudrun)

[![](https://github.com/user-attachments/assets/f3db034f-1b1b-4983-9f9a-06a3aeb1b64e)](https://colab.research.google.com/drive/1l1fAyDzNSSCVOF_JBpXRp2b3SHuI5bz6?usp=sharing)

マニュアル
https://motoki-design.co.jp/wordpress/xserver-vps-auto-renew/

Manual
https://motoki-design.co.jp/wordpress/xserver-vps-auto-renew/

手册
https://motoki-design.co.jp/wordpress/xserver-vps-auto-renew/

![Clipchamp7-ezgif com-video-to-gif-converter](https://github.com/user-attachments/assets/745a85ef-0d5a-4532-9774-3b7fcb2c8b52)

我制作了 Tampermonkey [Install](https://raw.githubusercontent.com/GitHub30/extend-vps-exp/refs/heads/main/renew.user.js) 然后，请访问：https://secure.xserver.ne.jp/xapanel/login/xvps/

如果不起作用，请设置 GitHub Actions 的 Secrets 环境变量。

```env
EMAIL=your@gmail.com
PASSWORD=yourpassword
AUTH_LOGIN_OTP=your_base32_totp_secret
PROXY_SERVER=http://user:password@example.com:8888
NOTICE_TG_ENABLED=true
NOTICE_TG_TOKEN=telegram_bot_token
NOTICE_TG_USERID=telegram_chat_id
NOTICE_DINGTALK_ENABLED=false
NOTICE_DINGTALK_WEBHOOK=https://oapi.dingtalk.com/robot/send?access_token=your_token
NOTICE_DINGTALK_SECRET=your_signing_secret
NOTICE_BARK_ENABLED=false
NOTICE_BARK_SERVER=https://api.day.app
NOTICE_BARK_DEVICE_KEY=your_device_key,another_device_key
NOTICE_BARK_GROUP=XServer-VPS
NOTICE_BARK_SOUND=
NOTICE_LARK_ENABLED=false
NOTICE_LARK_WEBHOOK=https://open.larksuite.com/open-apis/bot/v2/hook/your_hook
NOTICE_LARK_SECRET=your_signing_secret
DEBUG=true \
```

通知渠道按 Telegram → 钉钉 → Bark → Lark 的顺序串行发送。单个渠道发送失败不会阻断后续渠道；将对应的 `NOTICE_*_ENABLED` 设置为 `true` 即可启用。

`NOTICE_BARK_DEVICE_KEY` 支持填写多个 Bark 设备 key，使用英文逗号分隔；通知会依次发送到每个设备。

## 本地图片验证码识别

XServer 数字验证码现在由 Python 进程内的 TensorFlow/Keras 模型识别，不再调用公共 Cloud Run OCR API。模型固定放在 `captcha/xserver_captcha.keras`，程序使用相对于模块文件的绝对路径加载，因此不依赖启动时的工作目录。模型会延迟加载一次并在后续识别中复用。

依赖安装及测试：

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

测试使用参考仓库已有的合法样例确认模型加载、预处理、推理和数字 CTC decode 链路可执行，但样例没有标注真实答案，因此不代表已验证生产验证码识别准确率。

<details><summary>安装代理服务器</summary>

```bash
apt update
apt install -y tinyproxy
echo Allow 0.0.0.0/0 >> /etc/tinyproxy/tinyproxy.conf
echo BasicAuth user password >> /etc/tinyproxy/tinyproxy.conf
systemctl restart tinyproxy
systemctl status tinyproxy
```
</details>


```bash
# 依赖安装 playwright
apt update
apt install -y \
  libgtk-3-0 \
  libdbus-glib-1-2 \
  libxt6 \
  libx11-xcb1 \
  libxcomposite1 \
  libxdamage1 \
  libxrandr2 \
  libnss3 \
  libxss1 \
  libatk-bridge2.0-0 \
  libdrm2 \
  libgbm1 \
  libxshmfence1

apt install -y libasound2t64

apt install   libasound2  




```

我想去西門町，和大家一起喝珍珠奶茶。
