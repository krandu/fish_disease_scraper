#!/usr/bin/env python3
"""
Fish Disease Image Dataset Collector
=====================================
用途: 自动从多个公开图片源收集鱼病样本图片，用于 AI 识别数据集构建
支持: 本地运行 / GitHub Codespaces / GitHub Actions
输出: 按疾病分类整理的图片目录 + ZIP 压缩包

使用方法:
  pip install -r requirements.txt
  python fish_disease_scraper.py

可选参数:
  --diseases      指定要收集的疾病列表 (用逗号分隔)
  --max-per-class 每类最多收集图片数量 (默认: 50)
  --output-dir    输出目录 (默认: fish_disease_dataset)
  --sources       选择图片源: flickr,inat,gbif,bing (默认: 全部)
  --no-zip        不打包 ZIP
  --min-size      最小图片尺寸 px (默认: 200)
"""

import os
import sys
import time
import json
import zipfile
import hashlib
import logging
import argparse
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── 尝试导入可选依赖 ────────────────────────────────────────────────────────
try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

try:
    from PIL import Image
    import io as _io
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

# ── 日志配置 ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("fish-scraper")

# ═══════════════════════════════════════════════════════════════════════════════
# 配置区：鱼病关键词（英文）
# ═══════════════════════════════════════════════════════════════════════════════
FISH_DISEASES = {
    "白点病 (Ich)":            ["fish ich white spot disease", "ichthyophthirius multifiliis fish"],
    "烂尾病 (Fin Rot)":        ["fish fin rot disease", "bacterial fin rot fish"],
    "水霉病 (Saprolegnia)":    ["fish saprolegnia fungal infection", "fish cotton wool disease"],
    "细菌性溃疡":               ["fish bacterial ulcer disease", "fish skin ulcer lesion"],
    "锚头鳋 (Anchor Worm)":   ["fish anchor worm parasite lernaea", "lernaea fish parasite"],
    "鱼虱 (Fish Lice)":       ["argulus fish lice parasite", "fish louse disease"],
    "竖鳞病 (Dropsy)":        ["fish dropsy pinecone disease", "fish dropsy swollen scales"],
    "出血病 (Hemorrhage)":     ["fish hemorrhagic disease red spot", "fish bleeding disease"],
    "鳃病 (Gill Disease)":    ["fish gill disease infection", "fish gill pathology"],
    "眼病 (Pop-eye)":          ["fish popeye exophthalmia disease", "fish eye swelling disease"],
    "肠炎 (Enteritis)":       ["fish enteritis intestinal disease", "fish internal bacterial infection"],
    "传染性造血器官坏死":        ["infectious hematopoietic necrosis fish IHN", "salmon IHN virus disease"],
    "病毒性出血性败血症":        ["viral hemorrhagic septicemia fish VHS", "VHS fish disease"],
    "鲤春病毒血症":             ["spring viremia carp SVC disease", "carp SVC virus hemorrhage"],
    "健康鱼对照":               ["healthy aquarium fish", "healthy koi carp pond fish"],
}

# ═══════════════════════════════════════════════════════════════════════════════
# 图片来源配置
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_inat_images(query: str, max_count: int) -> list[dict]:
    """iNaturalist API — 开放生物多样性观测数据，免费无需 key"""
    results = []
    taxon_hint = "fishes"
    page, per_page = 1, min(max_count, 30)
    while len(results) < max_count:
        url = (
            "https://api.inaturalist.org/v1/observations?"
            f"q={urllib.parse.quote(query)}"
            f"&taxon_name={taxon_hint}"
            "&has[]=photos&quality_grade=research"
            f"&per_page={per_page}&page={page}"
            "&order=desc&order_by=created_at"
        )
        try:
            data = _json_get(url)
            items = data.get("results", [])
            if not items:
                break
            for obs in items:
                for photo in obs.get("photos", []):
                    url_large = photo.get("url", "").replace("square", "large")
                    if url_large:
                        results.append({
                            "url": url_large,
                            "source": "iNaturalist",
                            "obs_id": obs.get("id"),
                            "license": photo.get("license_code", "unknown"),
                        })
                        if len(results) >= max_count:
                            return results
            page += 1
            time.sleep(0.5)
        except Exception as e:
            log.debug(f"iNat error: {e}")
            break
    return results


def fetch_gbif_images(query: str, max_count: int) -> list[dict]:
    """GBIF — 全球生物多样性信息设施，开放数据"""
    results = []
    offset, limit = 0, min(max_count, 25)
    while len(results) < max_count:
        url = (
            "https://api.gbif.org/v1/occurrence/search?"
            f"q={urllib.parse.quote(query)}"
            "&mediaType=StillImage&kingdom=Animalia"
            f"&limit={limit}&offset={offset}"
        )
        try:
            data = _json_get(url)
            items = data.get("results", [])
            if not items:
                break
            for occ in items:
                for media in occ.get("media", []):
                    img_url = media.get("identifier", "")
                    if img_url and img_url.startswith("http"):
                        results.append({
                            "url": img_url,
                            "source": "GBIF",
                            "key": occ.get("key"),
                            "license": media.get("license", "unknown"),
                        })
                        if len(results) >= max_count:
                            return results
            offset += limit
            time.sleep(0.4)
        except Exception as e:
            log.debug(f"GBIF error: {e}")
            break
    return results


def fetch_flickr_images(query: str, max_count: int, api_key: str = "") -> list[dict]:
    """
    Flickr — 如果提供了 API key 则用官方 API，否则用公开 RSS
    环境变量: FLICKR_API_KEY
    """
    api_key = api_key or os.environ.get("FLICKR_API_KEY", "")
    results = []
    if api_key:
        page, per_page = 1, min(max_count, 50)
        while len(results) < max_count:
            url = (
                "https://www.flickr.com/services/rest/?"
                "method=flickr.photos.search"
                f"&api_key={api_key}"
                f"&text={urllib.parse.quote(query)}"
                "&license=1,2,3,4,5,6,9,10"  # CC 授权
                "&content_type=1&media=photos"
                f"&per_page={per_page}&page={page}"
                "&extras=url_l,url_m,license"
                "&format=json&nojsoncallback=1"
            )
            try:
                data = _json_get(url)
                photos = data.get("photos", {}).get("photo", [])
                if not photos:
                    break
                for p in photos:
                    img_url = p.get("url_l") or p.get("url_m", "")
                    if img_url:
                        results.append({
                            "url": img_url,
                            "source": "Flickr",
                            "photo_id": p.get("id"),
                            "license": p.get("license", "unknown"),
                        })
                        if len(results) >= max_count:
                            return results
                page += 1
                time.sleep(0.5)
            except Exception as e:
                log.debug(f"Flickr API error: {e}")
                break
    else:
        # 无 key 时用公开标签 RSS（数量有限）
        tag = urllib.parse.quote(query.replace(" ", ","))
        url = f"https://www.flickr.com/services/feeds/photos_public.gne?tags={tag}&format=json&nojsoncallback=1"
        try:
            data = _json_get(url)
            for item in data.get("items", [])[:max_count]:
                img = item.get("media", {}).get("m", "")
                if img:
                    results.append({
                        "url": img.replace("_m.", "_b."),
                        "source": "Flickr-RSS",
                        "license": "CC",
                    })
        except Exception as e:
            log.debug(f"Flickr RSS error: {e}")
    return results


def fetch_wikimedia_images(query: str, max_count: int) -> list[dict]:
    """Wikimedia Commons — 开放内容，免费"""
    results = []
    url = (
        "https://commons.wikimedia.org/w/api.php?"
        "action=query&generator=search&prop=imageinfo"
        f"&gsrsearch={urllib.parse.quote(query)}"
        f"&gsrnamespace=6&gsrlimit={min(max_count, 20)}"
        "&iiprop=url|extmetadata&iiurlwidth=800"
        "&format=json"
    )
    try:
        data = _json_get(url)
        pages = data.get("query", {}).get("pages", {})
        for page in pages.values():
            info = page.get("imageinfo", [{}])[0]
            img_url = info.get("thumburl") or info.get("url", "")
            if img_url and img_url.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                license_info = info.get("extmetadata", {}).get("License", {}).get("value", "unknown")
                results.append({
                    "url": img_url,
                    "source": "Wikimedia",
                    "page_id": page.get("pageid"),
                    "license": license_info,
                })
                if len(results) >= max_count:
                    break
    except Exception as e:
        log.debug(f"Wikimedia error: {e}")
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# 核心工具函数
# ═══════════════════════════════════════════════════════════════════════════════

def _json_get(url: str, timeout: int = 15) -> dict:
    """通用 JSON GET 请求"""
    headers = {"User-Agent": "FishDiseaseDatasetBot/1.0 (research; contact@example.com)"}
    if HAS_REQUESTS:
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    else:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())


def download_image(url: str, dest_path: Path, min_size: int = 200) -> bool:
    """下载单张图片，可选最小尺寸过滤"""
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; FishDiseaseBot/1.0)",
        "Accept": "image/*,*/*",
    }
    try:
        if HAS_REQUESTS:
            resp = requests.get(url, headers=headers, timeout=20, stream=True)
            resp.raise_for_status()
            content = resp.content
            content_type = resp.headers.get("Content-Type", "")
        else:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as r:
                content = r.read()
                content_type = r.headers.get("Content-Type", "")

        # 格式检查
        if not any(t in content_type for t in ["image/jpeg", "image/png", "image/webp", "image/"]):
            if not url.lower().split("?")[0].endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
                return False

        # 尺寸过滤 (需要 PIL)
        if HAS_PIL and min_size > 0:
            try:
                img = Image.open(_io.BytesIO(content))
                w, h = img.size
                if w < min_size or h < min_size:
                    return False
                # 转为 RGB JPEG 存储，统一格式
                if img.mode not in ("RGB", "L"):
                    img = img.convert("RGB")
                out = _io.BytesIO()
                img.save(out, format="JPEG", quality=90)
                content = out.getvalue()
                dest_path = dest_path.with_suffix(".jpg")
            except Exception:
                pass  # PIL 失败则直接存原文件

        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(content)
        return True

    except Exception as e:
        log.debug(f"Download failed [{url[:60]}]: {e}")
        return False


def safe_filename(name: str, idx: int, ext: str = ".jpg") -> str:
    h = hashlib.md5(name.encode()).hexdigest()[:6]
    return f"{idx:04d}_{h}{ext}"


def create_zip(dataset_dir: Path, output_path: Path) -> Path:
    log.info(f"📦 正在打包 ZIP: {output_path.name} ...")
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for f in sorted(dataset_dir.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(dataset_dir.parent))
    size_mb = output_path.stat().st_size / 1024 / 1024
    log.info(f"✅ ZIP 打包完成: {output_path} ({size_mb:.1f} MB)")
    return output_path


# ═══════════════════════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════════════════════

def collect_disease(
    disease_name: str,
    keywords: list[str],
    out_dir: Path,
    max_count: int,
    sources: list[str],
    min_size: int,
) -> dict:
    """收集单种鱼病的图片"""
    safe_name = disease_name.replace("/", "_").replace(" ", "_").replace("(", "").replace(")", "")
    class_dir = out_dir / safe_name
    class_dir.mkdir(parents=True, exist_ok=True)

    log.info(f"🔍 [{disease_name}] 开始搜索 (目标: {max_count} 张)")
    candidates = []

    for kw in keywords:
        per_src = max(max_count // max(len(sources), 1) + 5, 10)
        if "inat" in sources:
            candidates.extend(fetch_inat_images(kw, per_src))
        if "gbif" in sources:
            candidates.extend(fetch_gbif_images(kw, per_src))
        if "flickr" in sources:
            candidates.extend(fetch_flickr_images(kw, per_src))
        if "wikimedia" in sources:
            candidates.extend(fetch_wikimedia_images(kw, per_src))

    # 去重
    seen_urls = set()
    unique = []
    for c in candidates:
        if c["url"] not in seen_urls:
            seen_urls.add(c["url"])
            unique.append(c)

    log.info(f"  → 找到 {len(unique)} 个候选 URL，开始下载...")

    downloaded = 0
    metadata = []

    def _dl(item, idx):
        ext = ".jpg"
        fname = safe_filename(item["url"], idx, ext)
        dest = class_dir / fname
        if dest.exists():
            return item, fname, True
        ok = download_image(item["url"], dest, min_size)
        return item, dest.name, ok

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(_dl, item, i): item for i, item in enumerate(unique[:max_count * 2])}
        for fut in as_completed(futures):
            if downloaded >= max_count:
                break
            item, fname, ok = fut.result()
            if ok:
                downloaded += 1
                metadata.append({
                    "file": fname,
                    "url": item["url"],
                    "source": item.get("source", ""),
                    "license": item.get("license", "unknown"),
                })
                if downloaded % 10 == 0:
                    log.info(f"  ✓ [{disease_name}] 已下载 {downloaded}/{max_count}")

    # 保存元数据
    meta_path = class_dir / "_metadata.json"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump({"disease": disease_name, "count": downloaded, "images": metadata}, f, ensure_ascii=False, indent=2)

    log.info(f"  ✅ [{disease_name}] 完成: {downloaded} 张")
    return {"disease": disease_name, "count": downloaded, "dir": str(class_dir)}


def build_dataset_index(dataset_dir: Path, results: list[dict]):
    """生成数据集总索引文件"""
    index = {
        "created_at": datetime.now().isoformat(),
        "tool": "fish_disease_scraper",
        "total_images": sum(r["count"] for r in results),
        "classes": len(results),
        "class_distribution": {r["disease"]: r["count"] for r in results},
        "usage_note": (
            "本数据集仅供学术研究和 AI 模型训练使用。"
            "图片来自公开数据源 (iNaturalist, GBIF, Flickr CC, Wikimedia)，"
            "请遵守各来源的许可协议。"
        ),
    }
    index_path = dataset_dir / "dataset_index.json"
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)

    # 写 README
    readme = f"""# 🐟 Fish Disease Image Dataset

> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}  
> 总图片数: **{index['total_images']}** | 疾病类别: **{index['classes']}**

## 类别分布

| 疾病 | 图片数 |
|------|--------|
"""
    for d, c in index["class_distribution"].items():
        readme += f"| {d} | {c} |\n"

    readme += """
## 数据来源

- [iNaturalist](https://www.inaturalist.org) — 生物观测开放数据
- [GBIF](https://www.gbif.org) — 全球生物多样性信息设施
- [Flickr CC](https://www.flickr.com) — CC 授权图片
- [Wikimedia Commons](https://commons.wikimedia.org) — 开放图片库

## 使用许可

本数据集仅供**学术研究和 AI 模型训练**使用，请遵守各来源平台的许可协议。
商业用途请自行核查每张图片的具体授权信息（见各类别目录下的 `_metadata.json`）。
"""
    (dataset_dir / "README.md").write_text(readme, encoding="utf-8")
    log.info(f"📄 数据集索引已生成: {index_path}")


# ═══════════════════════════════════════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════════════════════════════════════

def parse_args():
    parser = argparse.ArgumentParser(
        description="🐟 鱼病图片数据集自动收集工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--diseases", help="指定疾病(中文名)，逗号分隔；不填则收集全部")
    parser.add_argument("--max-per-class", type=int, default=50, help="每类最多图片数 (默认: 50)")
    parser.add_argument("--output-dir", default="fish_disease_dataset", help="输出目录")
    parser.add_argument("--sources", default="inat,gbif,flickr,wikimedia",
                        help="图片源: inat,gbif,flickr,wikimedia (默认全部)")
    parser.add_argument("--no-zip", action="store_true", help="不打包 ZIP")
    parser.add_argument("--min-size", type=int, default=200, help="最小图片尺寸 px (需要 Pillow)")
    parser.add_argument("--flickr-key", default="", help="Flickr API Key (也可用环境变量 FLICKR_API_KEY)")
    return parser.parse_args()


def check_dependencies():
    missing = []
    if not HAS_REQUESTS:
        missing.append("requests")
    if not HAS_PIL:
        log.warning("⚠️  未安装 Pillow，图片尺寸过滤已禁用 (pip install Pillow)")
    if missing:
        log.error(f"缺少依赖: {', '.join(missing)}，请运行: pip install {' '.join(missing)}")
        sys.exit(1)


def main():
    args = parse_args()
    check_dependencies()

    # 初始化
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    sources = [s.strip().lower() for s in args.sources.split(",")]

    # 筛选疾病
    if args.diseases:
        selected_names = [d.strip() for d in args.diseases.split(",")]
        diseases = {k: v for k, v in FISH_DISEASES.items() if any(n in k for n in selected_names)}
        if not diseases:
            log.error("未匹配到任何疾病，请检查名称")
            sys.exit(1)
    else:
        diseases = FISH_DISEASES

    log.info("=" * 60)
    log.info("🐟 鱼病图片数据集收集工具启动")
    log.info(f"   疾病类别: {len(diseases)} 类")
    log.info(f"   每类上限: {args.max_per_class} 张")
    log.info(f"   图片来源: {sources}")
    log.info(f"   输出目录: {out_dir.resolve()}")
    log.info("=" * 60)

    results = []
    for disease_name, keywords in diseases.items():
        result = collect_disease(
            disease_name=disease_name,
            keywords=keywords,
            out_dir=out_dir,
            max_count=args.max_per_class,
            sources=sources,
            min_size=args.min_size if HAS_PIL else 0,
        )
        results.append(result)
        time.sleep(1)  # 礼貌延迟，避免触发限流

    # 生成索引
    build_dataset_index(out_dir, results)

    total = sum(r["count"] for r in results)
    log.info(f"\n{'='*60}")
    log.info(f"🎉 数据集构建完成! 共 {total} 张图片，{len(results)} 个类别")
    log.info(f"{'='*60}\n")

    # 打包 ZIP
    if not args.no_zip:
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        zip_path = Path(f"fish_disease_dataset_{ts}.zip")
        create_zip(out_dir, zip_path)
        print(f"\n📦 ZIP 文件已生成: {zip_path.resolve()}")
    else:
        print(f"\n📁 数据集目录: {out_dir.resolve()}")


if __name__ == "__main__":
    main()
