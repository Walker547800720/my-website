# 自动化焊接设备运维提醒系统

单设备课程演示网站。Python 3.12，Streamlit + SQLite，运行入口 app.py。

## Streamlit Community Cloud 发布

- Repository: Walker547800720/my-website
- Branch: welding-maintenance-demo
- Main file path: app.py
- Advanced settings / Python: 3.12
- App visibility: Public（让没有账号的访问者也能打开）

在 https://share.streamlit.io 登录并授权GitHub后，Create app，按上述设置部署。网站实际部署成功后，以平台返回的 https 公网地址为准，不要把GitHub源码页面或127.0.0.1当作网站地址。

这是独立部署分支，不修改原main分支网站。运行代码与本地验收版本一致，未上传旧设备库、演示库、截图或账号密钥。三个依赖版本已核对PyPI存在，最低Python均为3.11。

## 分享和数据

分享云端网站首页（不带 ?demo=...），不同浏览器会话将各自创建一台从0h、0次开始的演示。转发带demo编号的完整地址会共享同一轮数据及控制，适合共同观看，不适合各自独立操作。

初始25℃为模拟值。每2秒最多推进1个模拟小时，沿用温度、故障、维护及预警服务。结束本轮可下载CSV和会话记录。

尚未配置外部持久存储，不把云容器本地SQLite当作永久档案；需要保留的演示数据应及时下载。免费托管可能休眠，答辩前应预先打开确认。微信内置浏览器的网络可达性需要在部署完成后用手机实测。

## 本地复现

python -m pip install -r requirements.txt
python -m streamlit run app.py

所有指标为课程仿真数据，不连接真实焊机，不代表厂家保养规范或寿命预测。
