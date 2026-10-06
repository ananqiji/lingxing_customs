# 领星报关自动化

本项目用于从领星 API 拉取 FBA 发货计划和本地产品资料，并写入 MySQL：

- 发货计划写入 `customs_bill_parcels`
- 物料表写入 `customs_product`
- 支持本地调试、云服务器部署和定时任务

## 目录结构

```text
src/
  common/      # 领星客户端、缓存、Excel/MySQL 公共能力
  shipment/    # 发货计划：拉取、拼接、Excel、MySQL 写库
  product/     # 物料表：产品列表/详情、MySQL 写库
tests/         # 单元测试
docs/          # 领星接口截图和字段依据
scripts/       # Linux 部署、检查、手动运行、安装定时任务脚本
```

## 本地常用命令

安装依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

检查领星鉴权：

```powershell
.\.venv\Scripts\python.exe main.py --check-auth
```

发货计划写库，默认跑昨天和今天：

发货单列表按领星官方协议传整数 `status=1`，使用 `start_date/end_date` 和
`offset/length` 查询。旧配置 `LINGXING_SHIPMENT_STATUS=已发货` 会兼容转换为 `1`。
待发货、作废及缺少日期的单据不会进入按日同步；列表重复分页、详情请求失败或详情为空时中止任务，避免用不完整结果写入或清理数据库。

```powershell
.\.venv\Scripts\python.exe main.py --job shipment --write-db --debug-api
```

物料表增量写库，默认跑昨天和今天更新过的 SKU：

```powershell
.\.venv\Scripts\python.exe main.py --job product --write-db --debug-api
```

物料表首次全量重建：

```powershell
.\.venv\Scripts\python.exe main.py --job product --write-db --product-full-refresh --debug-api
```

测试：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q main.py src tests
```

## 火山云服务器部署

### 1. 购买服务器

推荐先买一台小规格云服务器：

- 系统：Ubuntu 22.04 LTS
- 规格：2 核 2G 起步，2 核 4G 更稳
- 系统盘：40G
- 带宽：1-3 Mbps 固定带宽
- 公网 IP：必须要，用于加入领星 API 白名单

安全组建议只开放 SSH `22` 端口；MySQL `3306` 不需要开放公网。

### 2. 初始化服务器

用 PowerShell 登录：

```powershell
ssh root@你的服务器公网IP
```

服务器执行：

```bash
apt update
apt install -y git python3 python3-venv python3-pip vim cron
```

把服务器公网 IP 添加到领星 API 白名单。

### 3. 拉代码并安装依赖

```bash
mkdir -p /opt/lingxing
cd /opt/lingxing
git clone https://github.com/Rain-am/lingxing_customs.git .
bash scripts/setup_linux.sh
```

### 4. 配置 `.env`

```bash
cp .env.example .env
vim .env
```

必须填写：

- `LINGXING_APP_ID`
- `LINGXING_APP_SECRET`
- `CUSTOMS_SHOP_MAPPING_URL`
- `CUSTOMS_SHOP_MAPPING_APP_KEY`
- `CUSTOMS_SHOP_MAPPING_APP_SECRET`
- `MYSQL_HOST`
- `MYSQL_PORT`
- `MYSQL_USER`
- `MYSQL_PASSWORD`
- `MYSQL_DATABASE`
- `MYSQL_TABLE=customs_bill_parcels`
- `MYSQL_PRODUCT_TABLE=customs_product`
- `CUSTOMS_PROTECTED_ROW_IDS`：可选，填入需要定时同步跳过 upsert 的报关行 `id`，多个值可用逗号分隔
- `CUSTOMS_PROTECTED_SHIPMENT_SKUS`：可选，填入需要定时同步跳过 upsert 的 `发货单号|SKU`，多个值可用逗号分隔
- `CUSTOMS_PROTECTED_SHIPMENT_BOXES`：可选，填入需要定时同步跳过 upsert 的 `发货单号|箱号`，多个值可用逗号分隔

如需把报关服务器检测到的数据维护问题推送到供应链平台，继续配置：

```env
CUSTOMS_ISSUE_REPORT_ENABLED=1
CUSTOMS_ISSUE_REPORT_URL=https://product.radiancewave.cn/api/internal/customs-data-issues/import
CUSTOMS_ISSUE_REPORT_TOKEN=与平台 CUSTOMS_ISSUE_IMPORT_TOKEN 相同的密钥
CUSTOMS_ISSUE_REPORT_TIMEOUT_SECONDS=15
```

启用后，发货计划同步会按 `来源 + 发货日期` 推送缺失字段快照；物料表同步会按全部启用 SKU 推送产品资料缺失快照。平台会把本次快照中已不存在的问题自动标记为已解决。

如果数据库仍需通过 SSH 隧道访问，配置：

```env
MYSQL_USE_SSH_TUNNEL=1
SSH_HOST=115.190.118.240
SSH_PORT=22
SSH_USER=root
SSH_PASSWORD=真实SSH密码
```

保留：

```env
CUSTOMS_SHOP_MAPPING_URL=https://www.foresight.radiancewave.cn/api/assetOpenApi/queryData
LINGXING_PRODUCT_BATCH_SIZE=100
LINGXING_PRODUCT_PAGE_SIZE=1000
CUSTOMS_SHOP_MAPPING_API_ID=aOeOfPzADG
CUSTOMS_SHOP_MAPPING_PAGE_SIZE=200
```

### 5. 手动验证

```bash
.venv/bin/python main.py --check-auth
bash scripts/check.sh
bash scripts/run_once.sh shipment
bash scripts/run_once.sh product
```

首次全量重建物料表时才运行：

```bash
bash scripts/run_once.sh product-full-refresh
```

### 6. 安装定时任务

```bash
bash scripts/install_cron.sh
crontab -l
```

默认安装：

- 发货计划：每天 07:00-22:55 每 5 分钟执行一次
- 物料表：每天 07:00-22:55 每 5 分钟执行一次

查看日志：

```bash
tail -f /opt/lingxing/logs/shipment-cron.log
tail -f /opt/lingxing/logs/product-cron.log
```

## 后续更新代码

本地代码推送 GitHub 后，服务器执行：

```bash
cd /opt/lingxing
git pull origin main
.venv/bin/python -m pip install -r requirements.txt
bash scripts/check.sh
bash scripts/run_once.sh shipment
bash scripts/run_once.sh product
```

## 注意事项

- `.env` 保存真实密钥和密码，不要提交到 GitHub。
- 服务器公网 IP 固定后，必须加入领星 API 白名单。
- 脚本只写数据，不自动建表或改表。
- 发货计划和物料表可以部署在同一台服务器上。
