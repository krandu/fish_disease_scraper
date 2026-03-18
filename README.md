# 🐟 Fish Disease Image Dataset Scraper

自动收集鱼病样本图片，用于 AI 识别模型训练数据集构建。

## 📦 项目结构

```
.
├── fish_disease_scraper.py       # 主脚本
├── requirements.txt              # 依赖
├── README.md                     # 本文档
└── .github/workflows/
    └── collect_fish_disease.yml  # GitHub Actions 自动化工作流
```

## 🦠 支持的鱼病类别（14类）

| 疾病 | 英文 |
|------|------|
| 白点病 | Ichthyophthirius (Ich) |
| 烂尾病 | Fin Rot |
| 水霉病 | Saprolegnia |
| 细菌性溃疡 | Bacterial Ulcer |
| 锚头鳋 | Anchor Worm (Lernaea) |
| 鱼虱 | Fish Lice (Argulus) |
| 竖鳞病 | Dropsy |
| 出血病 | Hemorrhagic Disease |
| 鳃病 | Gill Disease |
| 眼病 | Pop-eye (Exophthalmia) |
| 肠炎 | Enteritis |
| 传染性造血器官坏死 | IHN Virus |
| 病毒性出血性败血症 | VHS Virus |
| 鲤春病毒血症 | SVC Virus |
| 健康鱼对照 | Healthy Fish (Control) |

## 🚀 快速开始

### 方式一：本地运行

```bash
# 1. 克隆仓库
git clone https://github.com/YOUR_USERNAME/fish-disease-dataset
cd fish-disease-dataset

# 2. 安装依赖
pip install -r requirements.txt

# 3. 运行（默认收集全部14类，每类50张）
python fish_disease_scraper.py

# 4. 查看帮助
python fish_disease_scraper.py --help
```

### 方式二：GitHub Codespaces

1. 点击仓库页面 **Code → Codespaces → Create codespace**
2. 在终端执行：
   ```bash
   pip install -r requirements.txt
   python fish_disease_scraper.py
   ```
3. 下载生成的 ZIP：在文件浏览器中右键 → Download

### 方式三：GitHub Actions（推荐）

1. Fork 本仓库
2. 进入 **Actions** 标签页
3. 选择 **Fish Disease Dataset Collector**
4. 点击 **Run workflow** → 配置参数 → 运行
5. 完成后在 Artifacts 下载数据集 ZIP

## ⚙️ 参数说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--max-per-class` | 每类最多图片数量 | 50 |
| `--diseases` | 指定疾病（中文名, 逗号分隔） | 全部 |
| `--output-dir` | 输出目录 | fish_disease_dataset |
| `--sources` | 图片来源 | inat,gbif,flickr,wikimedia |
| `--no-zip` | 不打包 ZIP | False |
| `--min-size` | 最小图片尺寸 px | 200 |

## 🌐 数据来源

| 来源 | 是否需要 API Key | 说明 |
|------|----------------|------|
| **iNaturalist** | ❌ 无需 | 生物观测开放数据，质量较高 |
| **GBIF** | ❌ 无需 | 全球生物多样性数据 |
| **Wikimedia Commons** | ❌ 无需 | 开放图片库 |
| **Flickr** | ✅ 可选 | 有 key 数量更多；无 key 使用公开 RSS |

### 配置 Flickr API Key（可选）

1. 在 [Flickr App Garden](https://www.flickr.com/services/apps/create/) 申请免费 API Key
2. GitHub Actions: 在仓库 **Settings → Secrets** 添加 `FLICKR_API_KEY`
3. 本地: `export FLICKR_API_KEY=your_key_here`

## 📁 输出结构

```
fish_disease_dataset/
├── README.md
├── dataset_index.json            # 数据集总索引
├── 白点病_(Ich)/
│   ├── _metadata.json            # 图片元数据（URL、来源、授权）
│   ├── 0001_a3f2c1.jpg
│   └── ...
├── 烂尾病_(Fin_Rot)/
│   └── ...
└── ...

fish_disease_dataset_20240318_1430.zip   # 可下载的压缩包
```

## ⚠️ 使用注意

- 本工具仅供**学术研究和 AI 模型训练**使用
- 每张图片的具体授权见各类别目录下的 `_metadata.json`
- 建议使用前检查 `license` 字段确认版权许可
- 脚本内置请求延迟，请勿大量并发以避免触发来源网站限流

## 🛠️ 扩展开发

如需添加新疾病或新数据源，编辑脚本中的 `FISH_DISEASES` 字典或添加新的 `fetch_xxx_images()` 函数即可。
