## Card Backup - 更新日志

### 用户与权限
- 支持多用户账号体系（SQLite 存储，SHA256 密码哈希）
- admin 为管理员，拥有全部权限
- 普通用户仅可见/操作自己创建的设备和卡片
- 登录后 header 显示当前用户名
- admin 可见"用户管理"入口，可添加/删除用户

### 设备管理
- 支持自定义创建设备（Web 端输入设备名称）
- 已存在设备名提示"设备名已存在"
- 空设备（0 张卡片）正常显示，展开后引导上传

### 卡片上传
- 支持多选文件上传卡片数据
- 自动识别卡片类型（5 字节 → ID，256/512/1024/2048/4096 字节 → IC）
- 上传后自动关联到当前设备

### 卡片预览
- 点击卡片查看二进制数据
- Mifare Classic 1K/4K 自动按扇区/区块结构化展示
- 关键字段颜色高亮：UID（蓝）、Key A（红）、Access Bits（黄）、Key B（青）
- 全 00/全 FF 区块半透明显示
- 兼容 ChameleonUltra 文本格式 dump（`+Sector: ...`）

### Docker
- 基础镜像切换为 `python:3.11-alpine`（镜像更小）
- 源码挂载到 `./app:/app`，更新无需重建镜像
- `DATA_DIR` 环境变量控制数据目录

### 项目结构
```
backend/
  app/                  # 源码目录（挂载到容器）
    app.py              # Flask 应用（含 API + 内嵌管理面板）
    requirements.txt    # Python 依赖
  data/                 # 持久化数据（SQLite + bin 文件）
  Dockerfile
  docker-compose.yml
  build_image.sh        # 构建并导出镜像脚本
```
